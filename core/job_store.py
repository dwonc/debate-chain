"""
core/job_store.py — Improvement #2: Persistent Job Store + Resume

SQLite 기반 영속적 작업 저장소.
- 프로세스 재시작 후에도 작업/결과 유지
- 명시적 상태 머신
- optimistic locking (row version)
- 재시작 시 running → recovering 자동 처리
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ─── 상태 정의 ───
class JobStatus:
    QUEUED      = "queued"
    RUNNING     = "running"
    RECOVERING  = "recovering"   # 서버 재시작 후 미완료 작업
    CONVERGED   = "converged"
    FAILED      = "failed"
    ABORTED     = "aborted"

VALID_TRANSITIONS: dict[str, set[str]] = {
    JobStatus.QUEUED:     {JobStatus.RUNNING, JobStatus.ABORTED},
    JobStatus.RUNNING:    {JobStatus.CONVERGED, JobStatus.FAILED, JobStatus.RECOVERING},
    JobStatus.RECOVERING: {JobStatus.RUNNING, JobStatus.FAILED, JobStatus.ABORTED},
    JobStatus.CONVERGED:  set(),
    JobStatus.FAILED:     {JobStatus.QUEUED},   # retry 허용
    JobStatus.ABORTED:    set(),
}


@dataclass
class JobRecord:
    job_id:     str
    job_type:   str          # debate / pair2 / pair3 / pipeline / self_improve
    status:     str
    phase:      str = ""
    payload:    dict = field(default_factory=dict)
    result:     Optional[dict] = None
    error:      Optional[str] = None
    parent_id:  Optional[str] = None
    version:    int = 0
    created_at: str = ""
    updated_at: str = ""
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["payload"] = json.dumps(d["payload"], ensure_ascii=False)
        d["result"]  = json.dumps(d["result"],  ensure_ascii=False) if d["result"] else None
        return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteJobStore:
    """
    SQLite 기반 JobStore.
    thread-safe: connection per thread (check_same_thread=False + lock).
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id      TEXT PRIMARY KEY,
        job_type    TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'queued',
        phase       TEXT NOT NULL DEFAULT '',
        payload     TEXT NOT NULL DEFAULT '{}',
        result      TEXT,
        error       TEXT,
        parent_id   TEXT,
        version     INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        started_at  TEXT,
        finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
    CREATE INDEX IF NOT EXISTS idx_jobs_type    ON jobs(job_type);
    CREATE INDEX IF NOT EXISTS idx_jobs_parent  ON jobs(parent_id);

    CREATE TABLE IF NOT EXISTS job_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        data        TEXT NOT NULL DEFAULT '{}',
        ts          TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str | Path = "debate_chain.db"):
        self.db_path = Path(db_path)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._initialize()

    @contextmanager
    def _conn(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=10,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        yield self._local.conn

    def _initialize(self):
        with self._conn() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()
        self._recover_stale_jobs()

    def _recover_stale_jobs(self):
        """서버 재시작 시 running 상태를 recovering으로 변경"""
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE status=?",
                (JobStatus.RECOVERING, _now(), JobStatus.RUNNING),
            )
            conn.commit()

    # ─── CRUD ───

    def create(
        self,
        job_id: str,
        job_type: str,
        payload: dict | None = None,
        parent_id: str | None = None,
    ) -> JobRecord:
        now = _now()
        rec = JobRecord(
            job_id=job_id,
            job_type=job_type,
            status=JobStatus.QUEUED,
            payload=payload or {},
            parent_id=parent_id,
            created_at=now,
            updated_at=now,
        )
        d = rec.to_dict()
        with self._write_lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (job_id,job_type,status,phase,payload,result,error,parent_id,version,created_at,updated_at)
                   VALUES (:job_id,:job_type,:status,:phase,:payload,:result,:error,:parent_id,:version,:created_at,:updated_at)""",
                d,
            )
            conn.commit()
        self._log_event(job_id, "created", {"job_type": job_type})
        return rec

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def transition(
        self,
        job_id: str,
        new_status: str,
        phase: str = "",
        result: dict | None = None,
        error: str | None = None,
        expected_version: int | None = None,
    ) -> JobRecord:
        """
        상태 전이 (optimistic locking).
        expected_version 지정 시 버전 불일치면 ValueError.
        """
        rec = self.get(job_id)
        if rec is None:
            raise KeyError(f"Job {job_id} not found")

        allowed = VALID_TRANSITIONS.get(rec.status, set())
        if new_status not in allowed:
            raise ValueError(f"Invalid transition {rec.status} → {new_status}")

        if expected_version is not None and rec.version != expected_version:
            raise ValueError(f"Version conflict: expected {expected_version}, got {rec.version}")

        now = _now()
        updates: dict[str, Any] = {
            "status": new_status,
            "phase": phase or rec.phase,
            "version": rec.version + 1,
            "updated_at": now,
            "job_id": job_id,
        }
        if result is not None:
            updates["result"] = json.dumps(result, ensure_ascii=False)
        if error is not None:
            updates["error"] = error[:4000]
        if new_status == JobStatus.RUNNING and rec.started_at is None:
            updates["started_at"] = now
        if new_status in (JobStatus.CONVERGED, JobStatus.FAILED, JobStatus.ABORTED):
            updates["finished_at"] = now

        with self._write_lock, self._conn() as conn:
            conn.execute(
                """UPDATE jobs SET
                   status=:status, phase=:phase, version=:version, updated_at=:updated_at
                   {result_set} {error_set} {started_set} {finished_set}
                   WHERE job_id=:job_id
                """.format(
                    result_set=",result=:result"       if "result"     in updates else "",
                    error_set=",error=:error"          if "error"      in updates else "",
                    started_set=",started_at=:started_at" if "started_at" in updates else "",
                    finished_set=",finished_at=:finished_at" if "finished_at" in updates else "",
                ),
                updates,
            )
            conn.commit()

        self._log_event(job_id, f"→{new_status}", {"phase": phase})
        return self.get(job_id)

    def update_phase(self, job_id: str, phase: str):
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET phase=?, updated_at=? WHERE job_id=?",
                (phase, _now(), job_id),
            )
            conn.commit()

    def list_jobs(
        self,
        job_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[JobRecord]:
        q = "SELECT * FROM jobs WHERE 1=1"
        params: list = []
        if job_type:
            q += " AND job_type=?"; params.append(job_type)
        if status:
            q += " AND status=?"; params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    # ─── 이벤트 로그 ───

    def _log_event(self, job_id: str, event_type: str, data: dict = {}):
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO job_events (job_id,event_type,data,ts) VALUES (?,?,?,?)",
                (job_id, event_type, json.dumps(data), _now()),
            )
            conn.commit()

    def get_events(self, job_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── 유틸 ───

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        d = dict(row)
        d["payload"] = json.loads(d.get("payload") or "{}")
        d["result"]  = json.loads(d["result"]) if d.get("result") else None
        return JobRecord(**d)


# ─── 싱글턴 인스턴스 ───
_store: Optional[SQLiteJobStore] = None

def get_store(db_path: str | Path | None = None) -> SQLiteJobStore:
    global _store
    if _store is None:
        path = db_path or Path(__file__).parent.parent / "debate_chain.db"
        _store = SQLiteJobStore(path)
    return _store
