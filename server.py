"""
Horcrux Web Server v8
Adaptive 단일 진입점 — /api/horcrux/run 통합 엔드포인트
External modes: Auto / Fast / Standard / Full / Parallel
Internal engines: adaptive_fast/standard/full, debate_loop, planning_pipeline, pair_generation, self_improve
"""
import json
import subprocess
import os
import re
import sys
import time
import threading
import concurrent.futures
from datetime import datetime
from pathlib import Path

# Windows cp949 콘솔에서 유니코드 출력 크래시 방지
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# .env 자동 로딩 (R03: python-dotenv 사용, 기존 수동 파싱 대체)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    # dotenv 미설치 시 최소한의 수동 로딩 (fallback)
    _env_file = Path(__file__).parent / ".env"
    if _env_file.exists():
        with open(_env_file, "r", encoding="utf-8") as _ef:
            for _line in _ef:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k, _v = _k.strip(), _v.strip()
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v

from planning_v2 import register_planning_v2_routes
from deep_refactor import inject_callers as inject_drf_callers, deep_refactors, run_deep_refactor, create_state as create_drf_state
from flask import Flask, request, jsonify, render_template_string, Response
from core.security import validate_project_dir

app = Flask(__name__)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── P0-004: API key 인증 미들웨어 ──
import secrets as _secrets

_HORCRUX_API_KEY = os.environ.get("HORCRUX_API_KEY", "")
if not _HORCRUX_API_KEY:
    _HORCRUX_API_KEY = _secrets.token_urlsafe(32)
    os.environ["HORCRUX_API_KEY"] = _HORCRUX_API_KEY
    # 서버 시작 시 콘솔에 출력 (아래 __main__ 블록에서)

_AUTH_EXEMPT_PREFIXES = ("/api/threads", "/api/analytics", "/planning", "/")
_AUTH_EXEMPT_METHODS = ("GET", "HEAD", "OPTIONS")


@app.before_request
def _check_api_key():
    """쓰기/실행 route에 X-API-Key 헤더 검증. GET/읽기는 통과."""
    if request.method in _AUTH_EXEMPT_METHODS:
        return None
    if any(request.path == p or request.path.startswith(p + "/") for p in _AUTH_EXEMPT_PREFIXES if p != "/"):
        return None
    if request.path == "/":
        return None
    key = request.headers.get("X-API-Key", "")
    if key != _HORCRUX_API_KEY:
        return jsonify({"error": "Unauthorized — set X-API-Key header"}), 401

# R12: Gemini/Claude/Codex 모든 caller는 core.llm에서 관리

# ═══════════════════════════════════════════
# R17: PROMPTS → core/prompts.py로 추출
# ═══════════════════════════════════════════
from core.prompts import (
    GENERATOR_PROMPT, GENERATOR_IMPROVE_PROMPT, GENERATOR_IMPROVE_PROMPT_V2,
    CRITIC_PROMPT, SYNTHESIZER_PROMPT, SPLIT_PROMPT, SPLIT_PROMPT_WITH_ARTIFACT,
    PART_PROMPT, SELF_IMPROVE_PROMPT,
)

# ═══════════════════════════════════════════
# R19: UTILITIES/CRITIC → core/engine/critic.py로 추출
# ═══════════════════════════════════════════
from core.engine import (
    is_caller_error, extract_json, format_issues_compact, extract_score,
    check_convergence, normalize_critic_output, check_convergence_v2,
    build_revision_focus, build_compact_context_package, extract_debate_artifact,
)

# Phase 1: AI CALLERS v8 — 타임아웃/프롬프트 크기 완전 해결
# ===============================================
# 수정사항:
# 1. Claude: -p 인자 방식으로 통일 (stdin 혼용 버그 제거 — 폴더 감지 무한대기 원인)
# 2. --dangerously-skip-permissions: 폴더 권한 프롬프트 차단
# 3. 프롬프트 자동 truncation (12000자 초과 시 압축)
# 4. 타임아웃 시 6000자로 줄여서 1회 재시도
# 5. 기본 timeout 600 → 300으로 단축
# ===============================================
import platform
import shutil

_NPM = os.environ.get("CLI_BIN_DIR", "") or (
    os.path.join(os.environ.get("APPDATA", ""), "npm")
    if platform.system() == "Windows" else "/usr/local/bin"
)
# R12: MAX_PROMPT_CHARS/MAX_PROMPT_RETRY는 core.llm에서 관리

# ── R02: 입력 sanitize ──
def _sanitize_task(task: str) -> str:
    """format-string 메타문자 이스케이프 — prompt injection 방지."""
    return task.replace("{", "{{").replace("}", "}}")


# ── R07: 로그 저장 시 비밀 스크러빙 ──
from core.security import redact as _redact_secrets

def _save_log(log_path: Path, state: dict):
    """로그 저장 — API 키/토큰 등 민감 정보 제거 후 기록."""
    raw = json.dumps(state, ensure_ascii=False, indent=2)
    scrubbed = _redact_secrets(raw)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(scrubbed)

# ── R12: AI Callers → core/llm로 추출 ──
from core.llm import (
    call_claude, call_codex, call_gemini, call_gemini_fast,
    _call_aux_critic, _truncate_prompt, _truncate_for_aux,
    CLAUDE_MODELS, GEMINI_MODELS, GEMINI_FAST_MODELS,
    AUX_CRITIC_ENDPOINTS, AUX_MAX_PROMPT_CHARS,
    MAX_PROMPT_CHARS as _LLM_MAX_PROMPT, MAX_PROMPT_RETRY as _LLM_MAX_RETRY,
)




# ═══════════════════════════════════════════
# DEBATE ENGINE v7 — Global State
# P1-002: 글로벌 dict에 lock 추가 (race condition 방지)
# ═══════════════════════════════════════════
import threading as _threading
_state_lock = _threading.Lock()
debates = {}

# ── R18: Debate engine → core/engine/debate.py로 추출 ──
from core.engine import run_multi_critic, run_debate as _run_debate_engine, _maybe_auto_tune_scoring


def run_debate(debate_id, task, threshold, max_rounds, initial_solution="", claude_model="", vision_url=""):
    """R18: wrapper — state 주입 + 콜백 연결."""
    state = debates[debate_id]
    _run_debate_engine(
        debate_id, task, threshold, max_rounds,
        initial_solution=initial_solution, claude_model=claude_model, vision_url=vision_url,
        state=state, on_complete=_maybe_auto_tune_scoring, save_log_fn=_save_log,
    )


# PAIR MODE
# ═══════════════════════════════════════════
pairs = {}


def _save_pair_files(results: dict, output_dir: str) -> list:
    """
    pair 결과에서 files 배열 파싱 → output_dir 기준으로 자동 저장.
    """
    base = Path(output_dir)
    saved = []
    for part_id, result in results.items():
        if not isinstance(result, dict):
            continue
        files = result.get("files", [])
        if not files and "raw" in result:
            parsed = extract_json(result["raw"])
            if parsed:
                files = parsed.get("files", [])
        for f in files:
            rel_path = f.get("path", "")
            code     = f.get("code", "")
            if not rel_path or not code:
                continue
            target = base / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
            saved.append(str(target))
            print(f"[pair] 저장됨: {target}")
    return saved


AI_CALLERS = [
    ("Claude Opus 4.6", call_claude),
    ("Codex GPT-5.4", call_codex),
    ("Gemini", call_gemini),
]


def run_pair(pair_id, task, mode, extra_context="", artifact=None):
    state = pairs[pair_id]
    num_parts = 3 if mode == "pair3" else 2

    try:
        state["phase"] = "splitting"

        # Phase 3: 구조화된 아티팩트가 있으면 구조적으로 전달
        if artifact:
            split_raw = call_claude(SPLIT_PROMPT_WITH_ARTIFACT.format(
                num_parts=num_parts,
                task=task,
                artifact_score=artifact.get("score", 0),
                artifact_rounds=artifact.get("rounds", 0),
                final_solution_summary=artifact.get("final_solution", "")[:1500],
                key_decisions=", ".join(artifact.get("key_decisions", [])[:5]) or "N/A",
                remaining_concerns=", ".join(artifact.get("remaining_concerns", [])[:3]) or "None",
            ))
            ctx = ""
        else:
            ctx = ""
            if extra_context:
                # 긴 context 압축
                if len(extra_context) > 2000:
                    extra_context = extra_context[:2000] + "\n[...truncated]"
                ctx = f"\nAdditional context:\n{extra_context}"
            split_raw = call_claude(SPLIT_PROMPT.format(
                task=task, num_parts=num_parts, extra_context=ctx
            ))

        split_json = extract_json(split_raw)
        if not split_json or "parts" not in split_json:
            state["messages"].append({"role": "architect", "model": "Claude Opus 4.6", "content": split_raw})
            state["status"] = "error"
            state["error"] = f"Failed to split task. Claude raw response: {split_raw[:500]}"
            state["finished_at"] = datetime.now().isoformat()
            # early return 시에도 로그 저장
            try:
                log_file = LOG_DIR / f"{pair_id}.json"
                _save_log(log_file, state)
            except Exception: pass
            return

        shared_spec = json.dumps(split_json.get("shared_spec", {}), indent=2)
        parts = split_json["parts"][:num_parts]
        state["spec"] = json.dumps(split_json, indent=2)
        state["messages"].append({
            "role": "architect", "model": "Claude Opus 4.6",
            "content": json.dumps(split_json, indent=2)
        })

        if state.get("abort"):
            state["status"] = "aborted"; return

        state["phase"] = "parallel_gen"
        prompts = []
        for part in parts:
            prompts.append(PART_PROMPT.format(
                task=task,
                part_title=part.get("title", part.get("id", "")),
                part_description=part.get("description", ""),
                shared_spec=shared_spec,
                extra_context=ctx,
            ))

        PAIR_TIMEOUT = 1200  # 20min per part (code gen is heavy)
        PAIR_RETRY_CALLERS = [
            ("Claude", call_claude),
            ("Codex", call_codex),
            ("Gemini", call_gemini),
        ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_parts) as pool:
            futures = []
            for i, prompt in enumerate(prompts):
                if state.get("abort"):
                    break
                ai_name, ai_fn = AI_CALLERS[i % len(AI_CALLERS)]
                futures.append((parts[i], ai_name, ai_fn, prompt, pool.submit(ai_fn, prompt)))

            for part, ai_name, ai_fn, prompt, future in futures:
                if state.get("abort"):
                    break
                part_id = part.get("id", part.get("title", "unknown"))
                raw = None
                used_model = ai_name

                # 1차 시도 (timeout 포함)
                try:
                    raw = future.result(timeout=PAIR_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    print(f"[PAIR] {ai_name} timed out for {part_id}, retrying...")
                    raw = None
                except Exception as e:
                    print(f"[PAIR] {ai_name} error for {part_id}: {e}")
                    raw = None

                # 타임아웃/실패 시 다른 모델로 재시도
                if not raw or "[ERROR]" in (raw or ""):
                    for retry_name, retry_fn in PAIR_RETRY_CALLERS:
                        if retry_name == ai_name:
                            continue  # 같은 모델 스킵
                        print(f"[PAIR] Retrying {part_id} with {retry_name}...")
                        try:
                            raw = retry_fn(prompt, timeout=PAIR_TIMEOUT)
                            if raw and "[ERROR]" not in raw:
                                used_model = f"{retry_name} (retry)"
                                break
                        except Exception:
                            continue

                # 결과 저장 (실패해도 부분 결과 보존)
                pj = extract_json(raw) if raw else None
                status_label = "ok" if pj else "raw" if raw else "failed"
                state["messages"].append({
                    "role": part_id, "model": used_model,
                    "title": part.get("title", ""),
                    "status": status_label,
                    "content": json.dumps(pj, indent=2) if pj else (raw or f"[FAILED] {ai_name} and all retries failed for {part_id}")
                })
                state["results"][part_id] = pj or {"raw": raw or "", "status": status_label}

        if state.get("abort"):
            state["status"] = "aborted"; return
        state["status"] = "completed"

        # ── 자동 파일 저장 ──
        output_dir = state.get("output_dir", "")
        if output_dir:
            _save_pair_files(state["results"], output_dir)

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)

    if state.get("abort"):
        state["status"] = "aborted"
    state["finished_at"] = datetime.now().isoformat()
    log_file = LOG_DIR / f"{pair_id}.json"
    _save_log(log_file, state)

    if state["status"] == "completed":
        _maybe_auto_tune_scoring()


# ═══════════════════════════════════════════
# Phase 3: PIPELINES
# ═══════════════════════════════════════════
pipelines = {}    # debate_pair 파이프라인
self_improves = {}  # self_improve 루프


def run_debate_pair_pipeline(pipeline_id, task, pair_mode, threshold, max_rounds):
    """Phase 3: debate → pair 자동 파이프라인"""
    state = pipelines[pipeline_id]
    try:
        # Phase 1: Debate
        debate_id = pipeline_id + "_debate"
        debates[debate_id] = {
            "id": debate_id, "task": task, "status": "running",
            "round": 0, "phase": "", "messages": [], "raw_steps": [],
            "avg_score": 0, "final_solution": "",
            "error": None, "abort": False,
            "created_at": datetime.now().isoformat(), "finished_at": None,
        }
        state["debate_id"] = debate_id
        state["phase"] = "debate"

        run_debate(debate_id, task, threshold, max_rounds)

        debate_result = debates[debate_id]
        if debate_result["status"] not in ("converged", "max_rounds"):
            state["status"] = "error"
            state["error"] = f"Debate failed: {debate_result.get('error', debate_result['status'])}"
            return

        # Phase 2: 구조화된 아티팩트 추출
        artifact = extract_debate_artifact(debate_result)
        state["phase"] = "pair"

        pair_id = pipeline_id + "_pair"
        pairs[pair_id] = {
            "id": pair_id, "task": task, "mode": pair_mode, "status": "running",
            "phase": "splitting", "messages": [], "results": {}, "spec": "",
            "error": None, "abort": False,
            "created_at": datetime.now().isoformat(), "finished_at": None,
        }
        state["pair_id"] = pair_id

        run_pair(pair_id, task, pair_mode, artifact=artifact)

        state["status"] = pairs[pair_id]["status"]
        state["finished_at"] = datetime.now().isoformat()

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        state["finished_at"] = datetime.now().isoformat()


def run_self_improve(sid, task, iterations, initial_solution=""):
    """Phase 3: 자기개선 루프"""
    state = self_improves[sid]
    solution = initial_solution
    caller = call_claude  # self_improve는 Claude 기본

    try:
        for i in range(1, iterations + 1):
            if state.get("abort"): break
            state["iteration"] = i

            if i == 1 and not initial_solution:
                raw = caller(GENERATOR_PROMPT.format(task=task))
            else:
                raw = caller(SELF_IMPROVE_PROMPT.format(prev=solution, task=task))

            jd = extract_json(raw)
            if jd:
                solution = jd.get("solution", raw)
                weaknesses = jd.get("weaknesses", [])
                improvements = jd.get("improvements", [])
            else:
                solution = raw
                weaknesses, improvements = [], []

            state["messages"].append({
                "role": f"iteration_{i}",
                "content": solution,
                "weaknesses": weaknesses,
                "improvements": improvements,
            })

        # 최종 Critic 검증 (Codex — 다른 모델)
        state["phase"] = "final_critic"
        critic_raw = call_codex(CRITIC_PROMPT.format(
            task=task, solution=solution,
            previously_fixed="None (self-improve final check)"
        ))
        critic_data = extract_json(critic_raw) or {}
        state["final_score"] = extract_score(critic_data, critic_raw)
        state["final_solution"] = solution
        state["status"] = "completed"

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)

    state["finished_at"] = datetime.now().isoformat()
    log_file = LOG_DIR / f"{sid}.json"
    _save_log(log_file, state)

    if state["status"] == "completed":
        _maybe_auto_tune_scoring()


# ═══════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


# P3-003: Health/Ready endpoints
@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": "v8.3"})


@app.route("/ready")
def ready():
    """서비스 준비 상태 — DB/config 로드 확인."""
    checks = {}
    try:
        from core.adaptive.config import get_config
        cfg = get_config()
        checks["config"] = "ok"
    except Exception as e:
        checks["config"] = f"error: {e}"
    try:
        from core.job_store import get_store
        store = get_store()
        checks["job_store"] = "ok"
    except Exception as e:
        checks["job_store"] = f"error: {e}"
    all_ok = all(v == "ok" for v in checks.values())
    return jsonify({"ready": all_ok, "checks": checks}), 200 if all_ok else 503




@app.route("/api/start", methods=["POST"])
def start_debate():
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
    if not task: return jsonify({"error": "task required"}), 400
    debate_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    threshold = data.get("threshold", 8.0)
    max_rounds = data.get("max_rounds", 5)
    initial_solution = data.get("initial_solution", "")

    # Deep Dive: parent_debate_id가 있으면 final_solution 자동 이어받기
    parent_id = data.get("parent_debate_id", "")
    parent_task = ""
    if parent_id:
        parent = debates.get(parent_id)
        if not parent:
            log_file = LOG_DIR / f"{parent_id}.json"
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8") as f:
                    parent = json.load(f)
        if parent:
            if not initial_solution:
                initial_solution = parent.get("final_solution", "")
            if not task or task == parent.get("task", ""):
                parent_task = parent.get("task", "")
                task = task or parent_task
    # project_dir 지정 시 프로젝트 코드를 읽어 task에 context로 첨부 (R06: 경로 검증)
    project_dir = data.get("project_dir", "")
    if project_dir:
        try:
            validate_project_dir(project_dir)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        project_code = _read_project_files(project_dir)
        if project_code:
            task = (
                f"{task}\n\n"
                f"=== 현재 프로젝트 코드 ({project_dir}) ===\n"
                f"{project_code}\n"
                f"=== 위 코드를 분석하여 \uace0도화 포인트를 판단하라 ==="
            )

    debates[debate_id] = {
        "id": debate_id, "task": task, "status": "running",
        "round": 0, "phase": "", "messages": [], "raw_steps": [],
        "avg_score": 0, "final_solution": "",
        "error": None, "abort": False,
        "parent_debate_id": parent_id or None,
        "project_dir": project_dir,
        "created_at": datetime.now().isoformat(), "finished_at": None,
    }
    claude_model = CLAUDE_MODELS.get(data.get("claude_model", ""), "")
    debates[debate_id]["claude_model"] = claude_model

    t = threading.Thread(target=run_debate,
                         args=(debate_id, task, threshold, max_rounds, initial_solution, claude_model, data.get("vision_url", "")),
                         daemon=True)
    t.start()
    return jsonify({"debate_id": debate_id, "project_dir": project_dir, "claude_model": claude_model or "default"})


@app.route("/api/status/<debate_id>")
def get_status(debate_id):
    """compact metadata only"""
    state = debates.get(debate_id)
    if not state:
        log_file = LOG_DIR / f"{debate_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            debates[debate_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": state.get("id"),
        "task": state.get("task", ""),
        "status": state.get("status"),
        "round": state.get("round", 0),
        "phase": state.get("phase", ""),
        "avg_score": state.get("avg_score", 0),
        "message_count": len(state.get("messages", [])),
        "created_at": state.get("created_at"),
        "finished_at": state.get("finished_at"),
        "error": state.get("error"),
    })


@app.route("/api/result/<debate_id>")
def get_result(debate_id):
    """full result"""
    state = debates.get(debate_id)
    if not state:
        log_file = LOG_DIR / f"{debate_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            debates[debate_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify(state)


@app.route("/api/stop/<debate_id>", methods=["POST"])
def stop_debate(debate_id):
    state = debates.get(debate_id)
    if state: state["abort"] = True
    return jsonify({"ok": True})


@app.route("/api/threads")
def list_threads():
    threads = {}
    for f in sorted(LOG_DIR.glob("*.json"), reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            tid = d.get("id", f.stem)
            threads[tid] = {
                "id": tid, "task": d.get("task", "")[:80],
                "status": d.get("status", "unknown"),
                "avg_score": d.get("avg_score", 0), "round": d.get("round", 0),
                "created_at": d.get("created_at", ""),
            }
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    from planning_v2 import plannings as plan_v2_states
    with _state_lock:
        _all = {**debates, **pairs, **pipelines, **self_improves, **plan_v2_states, **horcrux_states, **deep_refactors}
    for tid, d in _all.items():
        threads[tid] = {
            "id": tid, "task": d.get("task", "")[:80],
            "status": d.get("status", "unknown"),
            "avg_score": d.get("avg_score", d.get("final_score", 0)),
            "round": d.get("round", d.get("iteration", 0)),
            "created_at": d.get("created_at", ""),
        }
    return jsonify(sorted(threads.values(), key=lambda x: x.get("created_at", ""), reverse=True))


@app.route("/api/timing/<job_id>")
def get_timing(job_id):
    """job 전체 소요시간 + phase별 breakdown"""
    state = (debates.get(job_id) or pairs.get(job_id)
             or pipelines.get(job_id) or self_improves.get(job_id))
    if not state:
        for store in [debates, pairs, pipelines, self_improves]:
            log_file = LOG_DIR / f"{job_id}.json"
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                break
    if not state:
        return jsonify({"error": "not found"}), 404

    created = state.get("created_at")
    finished = state.get("finished_at")
    total_sec = None
    if created and finished:
        from datetime import timezone
        def parse_dt(s):
            return datetime.fromisoformat(s)
        try:
            total_sec = round((parse_dt(finished) - parse_dt(created)).total_seconds(), 1)
        except (ValueError, TypeError):
            pass

    # message timestamp에서 phase별 소요시간 계산
    phases = []
    msgs = state.get("messages", [])
    for i, m in enumerate(msgs):
        ts = m.get("ts")
        next_ts = msgs[i+1].get("ts") if i+1 < len(msgs) else finished
        if ts and next_ts:
            try:
                dur = round((datetime.fromisoformat(next_ts) - datetime.fromisoformat(ts)).total_seconds(), 1)
                phases.append({"role": m["role"], "round": (i // 3) + 1, "duration_sec": dur, "ts": ts})
            except (ValueError, TypeError):
                pass

    return jsonify({
        "id": job_id,
        "status": state.get("status"),
        "created_at": created,
        "finished_at": finished,
        "total_sec": total_sec,
        "total_min": round(total_sec / 60, 1) if total_sec else None,
        "rounds": state.get("round", 0),
        "avg_score": state.get("avg_score", 0),
        "phase_breakdown": phases,
    })


@app.route("/api/delete/<debate_id>", methods=["DELETE"])
def delete_thread(debate_id):
    from planning_v2 import plannings as plan_v2_states
    debates.pop(debate_id, None)
    pairs.pop(debate_id, None)
    pipelines.pop(debate_id, None)
    self_improves.pop(debate_id, None)
    plan_v2_states.pop(debate_id, None)
    log_file = LOG_DIR / f"{debate_id}.json"
    if log_file.exists(): log_file.unlink()
    return jsonify({"ok": True})


# ── Vision UI Critic API ──

@app.route("/api/vision/analyze", methods=["POST"])
def vision_analyze():
    """
    이미지 파일 업로드 또는 URL → Vision UI Critic 분석.

    사용법 1 — 이미지 업로드:
      curl -X POST localhost:5000/api/vision/analyze -F "image=@screenshot.png"

    사용법 2 — URL 캡처 후 분석:
      curl -X POST localhost:5000/api/vision/analyze -H "Content-Type: application/json" \
           -d '{"url": "http://localhost:3000", "viewport": "desktop"}'

    사용법 3 — base64 직접 전달:
      curl -X POST localhost:5000/api/vision/analyze -H "Content-Type: application/json" \
           -d '{"image_base64": "iVBOR...", "viewport": "mobile"}'
    """
    from core.vision.critic import vision_ui_critic, run_vision_critic, analyze_image_file
    import base64 as _b64

    viewport = "desktop"
    rules_path = None

    # ── 방법 1: multipart 이미지 업로드 ──
    if "image" in request.files:
        f = request.files["image"]
        viewport = request.form.get("viewport", "desktop")
        rules_path = request.form.get("rules_path") or None
        img_bytes = f.read()
        img_b64 = _b64.b64encode(img_bytes).decode("ascii")
        result = vision_ui_critic(image_base64=img_b64, viewport=viewport, rules_path=rules_path)
        result["source"] = "upload"
        result["filename"] = f.filename
        return jsonify(result)

    # ── 방법 2/3: JSON body ──
    data = request.json or {}
    viewport = data.get("viewport", "desktop")
    rules_path = data.get("rules_path") or None
    color_scheme = data.get("color_scheme", "light")

    # base64 직접 전달
    if data.get("image_base64"):
        result = vision_ui_critic(image_base64=data["image_base64"], viewport=viewport, rules_path=rules_path)
        result["source"] = "base64"
        return jsonify(result)

    # URL 캡처 후 분석
    if data.get("url"):
        result = run_vision_critic(
            url=data["url"], viewport=viewport,
            color_scheme=color_scheme, rules_path=rules_path,
        )
        result["source"] = "url_capture"
        return jsonify(result)

    return jsonify({"error": "image file, image_base64, or url required"}), 400


@app.route("/api/vision/analyze/responsive", methods=["POST"])
def vision_analyze_responsive():
    """
    URL → 3종 뷰포트(desktop/tablet/mobile) 일괄 캡처 + 분석.

    curl -X POST localhost:5000/api/vision/analyze/responsive \
         -H "Content-Type: application/json" \
         -d '{"url": "http://localhost:3000"}'
    """
    from core.vision.capture import capture_responsive
    from core.vision.critic import vision_ui_critic

    data = request.json or {}
    url = data.get("url", "")
    if not url:
        return jsonify({"error": "url required"}), 400

    color_scheme = data.get("color_scheme", "light")
    rules_path = data.get("rules_path") or None

    captures = capture_responsive(url=url, color_scheme=color_scheme, healthcheck=True)

    results = {}
    for vp_name, cap in captures.items():
        if not cap["ok"]:
            results[vp_name] = {"ok": False, "error": cap["error"]}
            continue
        critique = vision_ui_critic(
            image_base64=cap["png_base64"], viewport=vp_name, rules_path=rules_path,
        )
        critique["viewport"] = vp_name
        results[vp_name] = critique

    return jsonify({"url": url, "color_scheme": color_scheme, "results": results})


# ── VIS-006/007: Reference Comparison ──

@app.route("/api/vision/compare", methods=["POST"])
def vision_compare():
    """
    VIS-007: 레퍼런스 이미지 vs 캡처 스크린샷 비교 평가.

    Body:
        url: 캡처할 URL
        project_dir: .horcrux/references/ 탐색할 프로젝트 경로
        reference_name: (선택) 특정 레퍼런스 파일명
        viewport: desktop|tablet|mobile (기본: desktop)
    """
    data = request.get_json(force=True)
    url = data.get("url", "")
    project_dir = data.get("project_dir", "")
    if not url or not project_dir:
        return jsonify({"error": "url and project_dir are required"}), 400
    try:
        validate_project_dir(project_dir)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    from core.vision.reference import run_comparison_critic
    result = run_comparison_critic(
        url=url,
        project_dir=project_dir,
        viewport=data.get("viewport", "desktop"),
        color_scheme=data.get("color_scheme", "light"),
        reference_name=data.get("reference_name"),
    )
    return jsonify(result)


@app.route("/api/vision/references", methods=["POST"])
def vision_list_references():
    """VIS-006: 프로젝트의 레퍼런스 이미지 목록 조회."""
    data = request.get_json(force=True)
    project_dir = data.get("project_dir", "")
    if not project_dir:
        return jsonify({"error": "project_dir is required"}), 400
    try:
        validate_project_dir(project_dir)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    from core.vision.reference import load_references
    refs = load_references(project_dir)
    # base64 제외하고 메타만 반환
    return jsonify([{
        "name": r["name"],
        "path": r["path"],
        "mime_type": r["mime_type"],
        "size_bytes": r["size_bytes"],
    } for r in refs])


@app.route("/api/test")
def test_connections():
    results = {}
    for name, fn in [("claude", call_claude), ("codex", call_codex)]:
        res = fn('Reply JSON only: {"status":"ok","model":"your_name"}')
        parsed = extract_json(res)
        results[name] = {
            "ok": "[ERROR]" not in res,
            "response": (json.dumps(parsed) if parsed else res[:200]),
            "json": parsed is not None,
        }
    return jsonify(results)


# ── Project-aware debate ──

def _read_project_files(project_dir: str, max_chars: int = 50000) -> str:
    """
    project_dir 아래 소스 + 설정 파일을 읽어서 텍스트로 반환.
    오진 방지: 설정 파일(.gitignore, config.json 등)을 우선 포함.
    max_chars 초과 시 파일 크기 순으로 중요도 높은 것만 포함.
    """
    base = Path(project_dir)
    if not base.exists():
        return ""

    SOURCE_EXTS = {".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".md",
                    ".java", ".kt", ".swift", ".vue", ".jsx", ".tsx", ".go", ".rs",
                    ".gradle", ".xml", ".properties", ".sql", ".sh", ".bat", ".css", ".scss"}
    SKIP_DIRS = {"__pycache__", ".venv", "venv", "node_modules", ".git", "dist", "build",
                  ".gradle", ".idea", "target", "out", "bin", "Pods", "DerivedData",
                  ".next", ".nuxt", "coverage", "jooq/bean"}

    # 1순위: 설정 파일 (오진 방지 핵심)
    config_files = []
    for name in [".gitignore", ".env.example", "config.json", "requirements.txt",
                 "package.json", "pyproject.toml", "Dockerfile", "docker-compose.yml",
                 "build.gradle", "settings.gradle", "pom.xml", "application.yml",
                 "Podfile", "Gemfile", "tsconfig.json", "vue.config.js",
                 ".gitlab-ci.yml", ".github/workflows/ci.yml", "Makefile"]:
        cf = base / name
        if cf.exists():
            config_files.append(cf)

    # 2순위: 소스 파일 (역할 기반 우선순위)
    # 비즈니스 로직 파일을 우선 포함하고, 테스트/정적 파일은 후순위
    PRIORITY_PATTERNS = {
        # 높은 우선순위: 비즈니스 로직
        "controller": 10, "service": 10, "dao": 10, "repository": 10,
        "store": 9, "actions": 9, "mutations": 9, "getters": 9,
        "router": 8, "api": 8, "util": 8, "helper": 8,
        "component": 7, "view": 7, "page": 7,
        # 낮은 우선순위
        "test": 1, "spec": 1, "mock": 1,
        "asset": 0, "static": 0, "font": 0, "image": 0,
    }
    SKIP_FILES = {".DS_Store", "Thumbs.db", "package-lock.json", "yarn.lock"}

    src_files = []
    for f in base.rglob("*"):
        if f.is_dir() or f in config_files:
            continue
        if f.name in SKIP_FILES:
            continue
        if any(skip in f.parts for skip in SKIP_DIRS):
            continue
        if f.suffix.lower() in SOURCE_EXTS:
            src_files.append(f)

    def _file_priority(f):
        name_lower = f.stem.lower()
        parts_lower = str(f).lower()
        score = 5  # 기본 점수
        for pattern, priority in PRIORITY_PATTERNS.items():
            if pattern in name_lower or pattern in parts_lower:
                score = max(score, priority)
                break
        return (-score, f.stat().st_size)  # 우선순위 높은 것 먼저, 같으면 큰 파일 먼저

    src_files.sort(key=_file_priority)

    chunks = []
    total = 0
    for f in config_files + src_files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            rel = str(f.relative_to(base))
            entry = f"\n### {rel}\n{content}"
            if total + len(entry) > max_chars:
                # 마지막 파일이라도 앞부분은 포함
                remain = max_chars - total - 100
                if remain > 500:
                    chunks.append(f"\n### {rel}\n{content[:remain]}\n[...truncated]")
                break
            chunks.append(entry)
            total += len(entry)
        except Exception:
            continue

    return "\n".join(chunks)


def _read_specific_files(project_dir: str, file_paths: list, max_chars: int = 30000) -> str:
    """분석 결과에서 언급된 특정 파일들만 읽어서 반환."""
    base = Path(project_dir)
    if not base.exists():
        return ""

    chunks = []
    total = 0
    for rel_path in file_paths:
        f = base / rel_path
        if not f.exists() or not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            entry = f"\n### {rel_path}\n{content}"
            if total + len(entry) > max_chars:
                remain = max_chars - total - 100
                if remain > 500:
                    chunks.append(f"\n### {rel_path}\n{content[:remain]}\n[...truncated]")
                break
            chunks.append(entry)
            total += len(entry)
        except Exception:
            continue
    return "\n".join(chunks)


def _extract_file_paths_from_solution(solution: str) -> list:
    """분석 결과 텍스트에서 파일 경로 추출."""
    import re
    paths = set()
    # JSON 형태: "files": ["path1", "path2"]
    for m in re.finditer(r'"files"\s*:\s*\[([^\]]+)\]', solution):
        for p in re.findall(r'"([^"]+)"', m.group(1)):
            if '.' in p and not p.startswith('http'):
                paths.add(p)
    # 마크다운 형태: `path/to/file.ext`
    for m in re.finditer(r'`([^`]+\.[a-zA-Z]{1,10})`', solution):
        p = m.group(1)
        if '/' in p or '\\' in p:
            paths.add(p)
    # ### path/to/file 형태
    for m in re.finditer(r'###?\s+(\S+\.[a-zA-Z]{1,10})', solution):
        paths.add(m.group(1))
    return list(paths)


def _verify_analysis(solution: str, project_dir: str) -> str:
    """
    2단계 검증: 분석 결과에서 언급된 파일들을 실제로 읽고,
    각 이슈가 진짜인지 검증.
    """
    if not solution or not project_dir:
        return solution

    file_paths = _extract_file_paths_from_solution(solution)
    if not file_paths:
        return solution

    # 언급된 파일들의 실제 코드 읽기
    verification_code = _read_specific_files(project_dir, file_paths, max_chars=30000)
    if not verification_code:
        return solution

    from core.llm.callers import call_claude
    verify_prompt = (
        f"아래는 코드 분석 결과와, 해당 분석에서 언급된 실제 소스 코드이다.\n\n"
        f"=== 분석 결과 ===\n{solution[:15000]}\n\n"
        f"=== 실제 소스 코드 ===\n{verification_code}\n\n"
        f"=== 검증 지시 ===\n"
        f"위 분석 결과의 각 이슈를 실제 코드와 대조하여:\n"
        f"1. 실제 코드에서 확인된 이슈 → [CONFIRMED] 태그\n"
        f"2. 코드에 존재하지 않거나 잘못된 진단 → [FALSE_POSITIVE] 태그 + 이유\n"
        f"3. 코드가 제공되지 않아 확인 불가 → [UNVERIFIED] 태그\n\n"
        f"원본 분석 결과의 구조를 유지하되, 각 이슈 앞에 검증 태그를 추가하고,\n"
        f"FALSE_POSITIVE인 항목은 제거하거나 취소선 처리해서 최종 검증 결과를 출력하라."
    )

    try:
        verified = call_claude(verify_prompt, timeout=120)
        if verified and not verified.startswith("[ERROR]"):
            return verified
    except Exception:
        pass

    return solution


# ── Pair ──

@app.route("/api/pair", methods=["POST"])
def start_pair():
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
    if not task: return jsonify({"error": "task required"}), 400
    mode = data.get("mode", "pair2")
    extra_context = data.get("context", "")
    output_dir = data.get("output_dir", "")  # 자동 파일 저장 경로
    pair_id = "pair_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    pairs[pair_id] = {
        "id": pair_id, "task": task, "mode": mode, "status": "running",
        "phase": "", "messages": [], "results": {}, "spec": "",
        "output_dir": output_dir,
        "error": None, "abort": False,
        "created_at": datetime.now().isoformat(), "finished_at": None,
    }
    t = threading.Thread(target=run_pair, args=(pair_id, task, mode, extra_context), daemon=True)
    t.start()
    return jsonify({"pair_id": pair_id, "mode": mode, "output_dir": output_dir})


@app.route("/api/pair/status/<pair_id>")
def pair_status(pair_id):
    """compact metadata only"""
    state = pairs.get(pair_id)
    if not state:
        log_file = LOG_DIR / f"{pair_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            pairs[pair_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": state.get("id"),
        "status": state.get("status"),
        "phase": state.get("phase", ""),
        "mode": state.get("mode", ""),
        "parts_done": len([m for m in state.get("messages", []) if m.get("role") != "architect"]),
        "message_count": len(state.get("messages", [])),
        "created_at": state.get("created_at"),
        "finished_at": state.get("finished_at"),
        "error": state.get("error"),
    })


@app.route("/api/pair/result/<pair_id>")
def pair_result_full(pair_id):
    """full result"""
    state = pairs.get(pair_id)
    if not state:
        log_file = LOG_DIR / f"{pair_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            pairs[pair_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify(state)


@app.route("/api/pair/stop/<pair_id>", methods=["POST"])
def pair_stop(pair_id):
    state = pairs.get(pair_id)
    if state: state["abort"] = True
    return jsonify({"ok": True})


# ── Phase 3: debate_pair 파이프라인 ──

@app.route("/api/debate_pair", methods=["POST"])
def start_debate_pair():
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
    if not task: return jsonify({"error": "task required"}), 400
    pair_mode = data.get("pair_mode", "pair2")
    threshold = data.get("threshold", 8.0)
    max_rounds = data.get("max_rounds", 3)

    pipeline_id = "dp_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    pipelines[pipeline_id] = {
        "id": pipeline_id, "task": task, "status": "running",
        "phase": "debate", "debate_id": None, "pair_id": None,
        "created_at": datetime.now().isoformat(), "finished_at": None, "error": None,
    }
    t = threading.Thread(
        target=run_debate_pair_pipeline,
        args=(pipeline_id, task, pair_mode, threshold, max_rounds),
        daemon=True
    )
    t.start()
    return jsonify({"pipeline_id": pipeline_id, "status": "running"})


@app.route("/api/pipeline/status/<pipeline_id>")
def pipeline_status(pipeline_id):
    state = pipelines.get(pipeline_id)
    if not state: return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": state["id"], "status": state["status"],
        "phase": state["phase"],
        "debate_id": state.get("debate_id"),
        "pair_id": state.get("pair_id"),
        "error": state.get("error"),
    })


@app.route("/api/pipeline/result/<pipeline_id>")
def pipeline_result(pipeline_id):
    state = pipelines.get(pipeline_id)
    if not state: return jsonify({"error": "not found"}), 404
    result = dict(state)
    did = state.get("debate_id")
    pid = state.get("pair_id")
    if did and did in debates:
        result["debate"] = {
            "status": debates[did].get("status"),
            "avg_score": debates[did].get("avg_score", 0),
            "round": debates[did].get("round", 0),
            "final_solution": debates[did].get("final_solution", ""),
        }
    if pid and pid in pairs:
        result["pair"] = {
            "status": pairs[pid].get("status"),
            "messages": pairs[pid].get("messages", []),
        }
    return jsonify(result)


# ── Phase 3: self_improve ──

@app.route("/api/self_improve", methods=["POST"])
def start_self_improve():
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
    debate_id = data.get("debate_id")  # 기존 debate 결과 이어받기
    iterations = data.get("iterations", 3)

    initial_solution = ""
    if debate_id and debate_id in debates:
        dstate = debates[debate_id]
        if dstate["status"] not in ("converged", "max_rounds"):
            return jsonify({"error": "debate not finished"}), 400
        task = task or dstate["task"]
        initial_solution = dstate.get("final_solution", "")

    if not task: return jsonify({"error": "task required"}), 400

    sid = "si_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    self_improves[sid] = {
        "id": sid, "task": task, "status": "running",
        "iteration": 0, "total_iterations": iterations,
        "messages": [], "final_solution": "", "final_score": 0,
        "phase": "improving", "parent_debate": debate_id,
        "created_at": datetime.now().isoformat(), "finished_at": None,
        "abort": False, "error": None,
    }
    t = threading.Thread(
        target=run_self_improve,
        args=(sid, task, iterations, initial_solution),
        daemon=True
    )
    t.start()
    return jsonify({"self_improve_id": sid})


@app.route("/api/self_improve/status/<sid>")
def self_improve_status(sid):
    state = self_improves.get(sid)
    if not state: return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": sid, "status": state["status"],
        "iteration": state["iteration"],
        "total_iterations": state["total_iterations"],
        "final_score": state.get("final_score", 0),
        "phase": state.get("phase", ""),
    })


@app.route("/api/self_improve/result/<sid>")
def self_improve_result(sid):
    state = self_improves.get(sid)
    if not state: return jsonify({"error": "not found"}), 404
    return jsonify(state)


# ── Phase 3: SSE 스트리밍 ──

@app.route("/api/stream/<job_id>")
def stream_status(job_id):
    """SSE: debate 또는 pair 실시간 상태 스트리밍"""
    def generate():
        while True:
            state = debates.get(job_id) or pairs.get(job_id) or pipelines.get(job_id)
            if not state:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                return
            payload = {
                "status": state.get("status"),
                "round": state.get("round", 0),
                "phase": state.get("phase", ""),
                "avg_score": state.get("avg_score", 0),
                "message_count": len(state.get("messages", [])),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if state["status"] != "running":
                yield f"data: {json.dumps({'event': 'done', 'status': state['status']})}\n\n"
                return
            time.sleep(1)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ═══════════════════════════════════════════
# HTML UI
# ═══════════════════════════════════════════

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Horcrux v7</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&family=JetBrains+Mono:wght@400;700&family=Noto+Sans+KR:wght@400;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d1a;color:#e0e0e0;font-family:'IBM Plex Sans','Noto Sans KR',sans-serif;height:100vh;overflow:hidden;display:flex}
.app{display:flex;flex:1;overflow:hidden}
.sidebar{width:280px;background:#0a0a18;border-right:1px solid #1a1a3a;display:flex;flex-direction:column;flex-shrink:0}
.sidebar-header{padding:16px;border-bottom:1px solid #1a1a3a;display:flex;align-items:center;gap:10px}
.sidebar-header h2{font-size:14px;font-weight:700;background:linear-gradient(135deg,#00e5ff,#da77f2);-webkit-background-clip:text;-webkit-text-fill-color:transparent;flex:1}
.btn-new{padding:6px 14px;background:linear-gradient(135deg,#00e5ff,#0099cc);border:none;border-radius:6px;color:#000;font-size:12px;font-weight:700;cursor:pointer}
.thread-list{flex:1;overflow-y:auto;padding:8px}.thread-list::-webkit-scrollbar{width:4px}.thread-list::-webkit-scrollbar-thumb{background:#333;border-radius:2px}
.thread-item{padding:10px 12px;border-radius:8px;cursor:pointer;margin-bottom:4px;border:1px solid transparent;transition:all .15s}
.thread-item:hover{background:#1a1a2e;border-color:#2a2a4a}.thread-item.active{background:#1a1a3a;border-color:#00e5ff44}
.thread-task{font-size:12px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:4px}
.thread-meta{display:flex;align-items:center;gap:6px;font-size:10px;color:#666}
.thread-status{display:inline-block;width:6px;height:6px;border-radius:50%}
.thread-status.running{background:#00e5ff;animation:pulse 1s infinite}.thread-status.converged{background:#69db7c}.thread-status.max_rounds{background:#ffd43b}.thread-status.error{background:#ff6b6b}.thread-status.completed{background:#69db7c}
.thread-score{font-family:'JetBrains Mono',monospace;font-weight:700}
.thread-delete{margin-left:auto;opacity:0;color:#ff6b6b;cursor:pointer;font-size:11px;padding:2px 6px;border-radius:4px}
.thread-item:hover .thread-delete{opacity:.6}.thread-delete:hover{opacity:1!important;background:#ff6b6b22}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.header{border-bottom:1px solid #1a1a3a;padding:14px 24px;display:flex;align-items:center;gap:14px;flex-shrink:0}
.header h1{font-size:18px;font-weight:700;background:linear-gradient(135deg,#00e5ff,#da77f2);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header p{font-size:11px;color:#555;letter-spacing:1px;text-transform:uppercase}
.roles{margin-left:auto;display:flex;gap:14px}.roles span{font-size:10px;font-weight:600;opacity:.6}
.content{flex:1;overflow-y:auto;padding:20px 24px}.content::-webkit-scrollbar{width:6px}.content::-webkit-scrollbar-thumb{background:#333;border-radius:3px}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:#444;gap:12px}
.empty-text{font-size:14px}.empty-sub{font-size:11px;color:#333;text-align:center;line-height:1.6}
.input-area{flex-shrink:0;border-top:1px solid #1a1a3a;padding:16px 24px;background:#0a0a16}
.input-row{display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;background:#12122a;border:1px solid #2a2a4a;border-radius:8px;color:#e0e0e0;font-size:13px;padding:10px 12px;resize:none;font-family:'IBM Plex Sans','Noto Sans KR',sans-serif;line-height:1.5;min-height:44px;max-height:120px}
textarea:focus{outline:none;border-color:#00e5ff;box-shadow:0 0 0 2px #00e5ff33}
.btn{padding:8px 20px;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;letter-spacing:.5px;white-space:nowrap}
.btn-run{background:linear-gradient(135deg,#00e5ff,#0099cc);color:#000;height:44px}.btn-run:disabled{background:#333;color:#666;cursor:not-allowed}
.btn-stop{background:#ff6b6b22;border:1px solid #ff6b6b55;color:#ff6b6b;height:44px}
.progress{margin-bottom:16px}.progress-info{display:flex;justify-content:space-between;margin-bottom:6px;font-size:11px;font-family:'JetBrains Mono',monospace}
.progress-label{color:#888}.progress-score{font-weight:700}
.progress-bar{height:3px;background:#2a2a4a;border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#00e5ff,#da77f2);border-radius:2px;transition:width .5s ease}
.msg{margin-bottom:14px;padding-left:14px;animation:fadeSlide .3s ease}
.msg-generator{border-left:3px solid #00e5ff}.msg-critic{border-left:3px solid #ff6b6b}.msg-synthesizer{border-left:3px solid #da77f2}
.msg-header{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.role-tag{display:inline-flex;align-items:center;gap:4px;border-radius:5px;padding:2px 8px;font-size:11px;font-weight:700;letter-spacing:.3px}
.role-generator{background:#00e5ff18;border:1px solid #00e5ff44;color:#00e5ff}.role-critic{background:#ff6b6b18;border:1px solid #ff6b6b44;color:#ff6b6b}
.role-synthesizer{background:#da77f218;border:1px solid #da77f244;color:#da77f2}
.score{display:inline-flex;border-radius:5px;padding:2px 8px;font-size:12px;font-weight:800;font-family:'JetBrains Mono',monospace}
.score-pass{background:#69db7c22;border:1px solid #69db7c55;color:#69db7c}.score-fail{background:#ff6b6b22;border:1px solid #ff6b6b55;color:#ff6b6b}
.msg pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.6;color:#d4d4d4;font-family:'JetBrains Mono',monospace;background:#1a1a2e;border-radius:8px;padding:14px;max-height:800px;overflow:auto;border:1px solid #2a2a4a}
.round-divider{display:flex;align-items:center;gap:12px;margin:20px 0 16px;color:#555;font-size:11px;font-family:'JetBrains Mono',monospace}
.round-divider::before,.round-divider::after{content:'';flex:1;height:1px;background:#1a1a3a}
.result{margin-top:16px;padding:16px;border-radius:10px;animation:fadeSlide .4s ease}
.result-ok{background:#69db7c0a;border:1px solid #69db7c33}.result-fail{background:#ff6b6b0a;border:1px solid #ff6b6b33}
.result-header{display:flex;align-items:center;gap:10px}.result-icon{font-size:24px}.result-title{font-size:15px;font-weight:700}.result-sub{font-size:11px;color:#888}
.btn-copy{margin-left:auto;padding:5px 14px;background:#2a2a4a;border:1px solid #3a3a5a;border-radius:6px;color:#aaa;font-size:11px;cursor:pointer}.btn-copy:hover{background:#3a3a5a;color:#ddd}
.test-btn{margin-top:12px;padding:8px 20px;background:#2a2a4a;border:1px solid #3a3a5a;border-radius:8px;color:#aaa;font-size:12px;cursor:pointer}.test-btn:hover{background:#3a3a5a;color:#ddd}
@keyframes fadeSlide{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<div class="app">
<div class="sidebar">
  <div class="sidebar-header"><h2>Horcrux v7</h2><button class="btn-new" onclick="newThread()">+ New</button></div>
  <div class="thread-list" id="threadList"></div>
</div>
<div class="main">
  <div class="header">
    <div><h1>Horcrux v7</h1><p>Multi-Critic · Regression · Pipeline</p></div>
    <div class="roles" id="rolesInfo">
      <span style="color:#00e5ff">Claude (Gen)</span>
      <span style="color:#ff6b6b">Codex+Gemini+Aux (Critics)</span>
      <span style="color:#da77f2">Codex (Synth)</span>
    </div>
  </div>
  <div class="content" id="content">
    <div class="empty" id="emptyState">
      <div class="empty-text" id="emptyTitle">New debate</div>
      <div class="empty-sub" id="emptySub">Multi-Critic(Codex+Gemini) · Regression detection · Multidimensional convergence<br>Synthesizer=Codex (different model from Generator)</div>
      <button class="test-btn" onclick="testConnections()">Test connections</button>
      <div id="testResult" style="margin-top:12px;font-size:12px;font-family:'JetBrains Mono',monospace;max-width:500px"></div>
    </div>
    <div id="progressArea" style="display:none" class="progress">
      <div class="progress-info"><span id="progressLabel" class="progress-label"></span><span id="progressScore" class="progress-score"></span></div>
      <div class="progress-bar"><div id="progressFill" class="progress-fill" style="width:0%"></div></div>
    </div>
    <div id="messages"></div>
    <div id="resultArea"></div>
  </div>
  <div class="input-area">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
      <div style="display:flex;gap:4px;margin-right:8px">
        <button id="modeAuto" class="btn" onclick="setMode('auto')" style="padding:4px 12px;font-size:11px;background:linear-gradient(135deg,#bc8cff,#58a6ff);color:#000">Auto</button>
        <button id="modeFast" class="btn" onclick="setMode('fast')" style="padding:4px 12px;font-size:11px;background:#2a2a4a;color:#888;border:1px solid #3a3a5a">Fast</button>
        <button id="modeStandard" class="btn" onclick="setMode('standard')" style="padding:4px 12px;font-size:11px;background:#2a2a4a;color:#888;border:1px solid #3a3a5a">Standard</button>
        <button id="modeFull" class="btn" onclick="setMode('full')" style="padding:4px 12px;font-size:11px;background:#2a2a4a;color:#888;border:1px solid #3a3a5a">Full</button>
        <button id="modeParallel" class="btn" onclick="setMode('parallel')" style="padding:4px 12px;font-size:11px;background:#2a2a4a;color:#888;border:1px solid #3a3a5a">Parallel</button>
      </div>
      <label style="font-size:11px;color:#888;font-weight:600">Claude</label>
      <select id="modelSelect" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:12px;padding:4px 8px;font-family:'JetBrains Mono',monospace;cursor:pointer">
        <option value="">Auto (default)</option>
        <option value="opus">Opus 4.6</option>
        <option value="sonnet">Sonnet 4.6</option>
      </select>
      <div id="autoOpts" style="display:flex;gap:8px;align-items:center;margin-left:4px">
        <select id="autoScope" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Scope">
          <option value="auto">Scope: Auto</option>
          <option value="small">Small</option>
          <option value="medium">Medium</option>
          <option value="large">Large</option>
        </select>
        <select id="autoRisk" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Risk">
          <option value="auto">Risk: Auto</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
        <select id="autoArtifact" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Artifact">
          <option value="none">No Artifact</option>
          <option value="ppt">PPT</option>
          <option value="pdf">PDF</option>
          <option value="doc">Doc</option>
        </select>
      </div>
      <div id="parallelOpts" style="display:none;gap:8px;align-items:center;margin-left:4px">
        <select id="parallelParts" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Parts">
          <option value="2">2 AI</option>
          <option value="3">3 AI</option>
        </select>
        <input id="outputDir" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px;width:140px" placeholder="Output Dir (optional)">
      </div>
      <div id="fullOpts" style="display:none;gap:8px;align-items:center;margin-left:4px">
        <select id="fullArtifact" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Artifact">
          <option value="none">No Artifact</option>
          <option value="ppt">PPT</option>
          <option value="pdf">PDF</option>
          <option value="doc">Doc</option>
        </select>
        <input id="fullAudience" value="general" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px;width:70px" placeholder="Audience">
        <select id="fullTone" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Tone">
          <option value="professional">Pro</option>
          <option value="casual">Casual</option>
          <option value="technical">Tech</option>
        </select>
      </div>
    </div>
    <div class="input-row">
      <textarea id="taskInput" rows="1" placeholder="Enter task... (Enter to run, Shift+Enter for newline)" oninput="autoGrow(this)"></textarea>
      <button id="btnStop" class="btn btn-stop" style="display:none" onclick="stopRun()">Stop</button>
      <button id="btnRun" class="btn btn-run" onclick="startRun()">Run</button>
    </div>
  </div>
</div>
</div>
<script>
const ROLES={generator:{name:"Generator(Claude)",cls:"generator"},critic:{name:"Critic(Codex+Gemini+Aux)",cls:"critic"},synthesizer:{name:"Synthesizer(Codex)",cls:"synthesizer"},final:{name:"Final Polish(Codex)",cls:"synthesizer"}};
const THRESHOLD=8.0,MAX_ROUNDS=5;
let cid=null,pt=null,lmc=0,run=false,curMode='auto';
function autoGrow(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px'}
document.getElementById("taskInput").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();startRun()}});
async function testConnections(){const el=$('testResult');el.innerHTML='Testing...';try{const r=await fetch("/api/test");const d=await r.json();el.innerHTML=Object.entries(d).map(([k,v])=>{const c=v.ok?'#69db7c':'#ff6b6b';return `<div style="color:${c};margin:6px 0;padding:8px;background:${c}11;border:1px solid ${c}33;border-radius:6px"><b>${v.ok?'OK':'FAIL'} ${k} ${v.json?'JSON ok':'no JSON'}</b></div>`}).join('')}catch(e){el.innerHTML=`<span style="color:#ff6b6b">${e.message}</span>`}}
async function loadThreads(){try{const r=await fetch("/api/threads");const t=await r.json();const el=$('threadList');if(!t.length){el.innerHTML='<div style="padding:20px;text-align:center;color:#444;font-size:12px">No tasks yet</div>';return}const typeBadge=(id)=>{if(id.startsWith('drf_'))return'<span style="font-size:9px;padding:1px 4px;border-radius:3px;background:#da77f222;color:#da77f2;border:1px solid #da77f255;margin-right:4px">DRF</span>';if(id.startsWith('plan_'))return'<span style="font-size:9px;padding:1px 4px;border-radius:3px;background:#58a6ff22;color:#58a6ff;border:1px solid #58a6ff55;margin-right:4px">PLAN</span>';if(id.startsWith('pair_'))return'<span style="font-size:9px;padding:1px 4px;border-radius:3px;background:#388bfd22;color:#388bfd;border:1px solid #388bfd55;margin-right:4px">PAIR</span>';if(id.startsWith('dp_'))return'<span style="font-size:9px;padding:1px 4px;border-radius:3px;background:#ffd43b22;color:#ffd43b;border:1px solid #ffd43b55;margin-right:4px">PIPE</span>';if(id.startsWith('si_'))return'<span style="font-size:9px;padding:1px 4px;border-radius:3px;background:#3fb95022;color:#3fb950;border:1px solid #3fb95055;margin-right:4px">SI</span>';if(id.startsWith('hrx_')||id.startsWith('adp_'))return'<span style="font-size:9px;padding:1px 4px;border-radius:3px;background:#00e5ff22;color:#00e5ff;border:1px solid #00e5ff55;margin-right:4px">HRX</span>';return''};el.innerHTML=t.map(t=>{const a=t.id===cid?'active':'';const sc=t.avg_score>=THRESHOLD?'#69db7c':t.avg_score>0?'#ff6b6b':'#666';const tm=t.created_at?new Date(t.created_at).toLocaleString('ko-KR',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';return `<div class="thread-item ${a}" onclick="selectThread('${t.id}')"><div class="thread-task">${typeBadge(t.id)}${esc(t.task)}</div><div class="thread-meta"><span class="thread-status ${t.status}"></span><span>${t.status}</span><span>R${t.round}</span><span class="thread-score" style="color:${sc}">${t.avg_score>0?t.avg_score.toFixed(1):'-'}</span><span style="margin-left:auto;color:#555">${tm}</span><span class="thread-delete" onclick="event.stopPropagation();deleteThread('${t.id}')">x</span></div></div>`}).join('')}catch(e){console.error('loadThreads error:',e);$('threadList').innerHTML='<div style="padding:20px;text-align:center;color:#ff6b6b;font-size:12px">Thread list error: '+e.message+'</div>'}}
function statusUrl(id){if(id.startsWith('plan_'))return`/api/planning/status/${id}`;if(id.startsWith('pair_'))return`/api/pair/status/${id}`;if(id.startsWith('dp_'))return`/api/pipeline/status/${id}`;if(id.startsWith('si_'))return`/api/self_improve/status/${id}`;if(id.startsWith('drf_'))return`/api/horcrux/status/${id}`;if(id.startsWith('hrx_'))return`/api/horcrux/status/${id}`;if(id.startsWith('adp_'))return`/api/horcrux/status/${id}`;return`/api/status/${id}`}
function resultUrl(id){if(id.startsWith('plan_'))return`/api/planning/result/${id}`;if(id.startsWith('pair_'))return`/api/pair/result/${id}`;if(id.startsWith('dp_'))return`/api/pipeline/result/${id}`;if(id.startsWith('si_'))return`/api/self_improve/result/${id}`;if(id.startsWith('drf_'))return`/api/horcrux/result/${id}`;if(id.startsWith('hrx_'))return`/api/horcrux/result/${id}`;if(id.startsWith('adp_'))return`/api/horcrux/result/${id}`;return`/api/result/${id}`}
async function selectThread(id){if(pt)clearInterval(pt);cid=id;lmc=0;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';
  const isP=id.startsWith('plan_');const isPair=id.startsWith('pair_');const isHrx=id.startsWith('hrx_')||id.startsWith('adp_');setMode(isP||isHrx?'auto':isPair?'parallel':'auto');
  const sr=await fetch(statusUrl(id));const s=await sr.json();
  if(!sr.ok||s.error==='not found'){loadThreads();return}
  if(s.status==='running'){
    $('taskInput').value=s.task||'';
    run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';
    pt=setInterval(poll,1500);
  } else {
    const fr=await fetch(resultUrl(id));const full=await fr.json();
    $('taskInput').value=full.task||'';
    renderAll(full);
    run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';renderResult(full);
  }
  loadThreads();}
function renderAll(s){const c=$('messages');c.innerHTML='';let cr=0;const isPlanning=!!s.final_plan||(s.id||'').startsWith('plan_');const isPair=(s.id||'').startsWith('pair_');(s.messages||[]).forEach(m=>{if(isPair){const pairRoles={architect:{name:'Architect (Claude)',cls:'synthesizer'},part1:{name:'Part 1',cls:'generator'},part2:{name:'Part 2',cls:'critic'},part3:{name:'Part 3',cls:'synthesizer'}};const pr=pairRoles[m.role]||{name:m.role,cls:'generator'};const label=m.label||pr.name;const statusTag=m.status?` <span style="font-size:10px;color:${m.status==='ok'?'#69db7c':'#ffd43b'}">[${m.status}]</span>`:'';const modelTag=m.model?` <span style="font-size:10px;color:#555">(${m.model})</span>`:'';if(m.role==='architect'){c.innerHTML+=`<div class="round-divider">Task Splitting</div>`}else if(m.role==='part1'){c.innerHTML+=`<div class="round-divider">Parallel Generation</div>`}c.innerHTML+=`<div class="msg msg-${pr.cls}"><div class="msg-header"><span class="role-tag role-${pr.cls}">${label}</span>${modelTag}${statusTag}</div><pre>${esc(m.content)}</pre></div>`;return}if(!isPlanning&&m.role==='generator'){cr++;c.innerHTML+=`<div class="round-divider">Round ${cr}</div>`}if(isPlanning){const phaseMap={generator:'Phase 1: Generate',synthesizer:'Phase 2: Synthesize',critic:'Phase 3: Critique',final:'Phase 4: Final Polish'};const ph=phaseMap[m.role];if(ph&&!c.innerHTML.includes(ph)){c.innerHTML+=`<div class="round-divider">${ph}</div>`}}const label=m.label||((ROLES[m.role]||{}).name)||m.role;const cls=(ROLES[m.role]||{cls:'generator'}).cls;let sh='';if(m.score!==undefined){const p=m.score>=THRESHOLD;sh=`<span class="score ${p?'score-pass':'score-fail'}">${Number(m.score).toFixed(1)}/10</span>`}const modelTag=m.model?` <span style="font-size:10px;color:#555">(${m.model})</span>`:'';c.innerHTML+=`<div class="msg msg-${cls}"><div class="msg-header"><span class="role-tag role-${cls}">${label}</span>${modelTag}${sh}</div><pre>${esc(m.content)}</pre></div>`});lmc=(s.messages||[]).length;sb()}
function renderResult(s){if(s.status!=='converged'&&s.status!=='max_rounds'&&s.status!=='completed')return;const ok=s.status==='converged'||s.status==='completed';const isP=(s.id||'').startsWith('plan_');const isPair=(s.id||'').startsWith('pair_');const isDrf=(s.id||'').startsWith('drf_');const isDebate=!isP&&!isPair&&!isDrf&&!(s.id||'').startsWith('hrx_')&&!(s.id||'').startsWith('si_');const deepBtn=(isDebate&&s.final_solution)?`<button class="btn-copy" style="background:#da77f222;border-color:#da77f255;color:#da77f2" onclick="deepDive('${s.id}')">Deep Dive</button>`:'';let sub;if(isPair){const partCount=Object.keys(s.results||{}).length;sub=`${s.mode||'pair2'} · ${partCount} parts generated · Parallel speed`}else if(isP){sub=`4 phases · Avg critic: ${(s.avg_score||0).toFixed(1)}/10`}else if(isDrf){sub=`Deep Refactor · ${s.round||0} rounds · Score: ${(s.avg_score||0).toFixed(1)}/10`}else{sub=`${s.round||0} rounds · Score: ${(s.avg_score||0).toFixed(1)}/10${s.parent_debate_id?` · (child of ${s.parent_debate_id})`:''}`}let solHtml='';if(s.final_solution){let sol=s.final_solution;if(isDrf){try{const j=typeof sol==='string'?JSON.parse(sol):sol;sol='Total issues: '+(j.total_issues||0)+'\n\n';(j.implementation_phases||[]).forEach(p=>{sol+=p.phase+'\n';(p.issues||[]).forEach(i=>{sol+='  ['+i.severity+'] '+i.id+': '+i.description.substring(0,120)+'\n'});sol+='\n'})}catch(e){}}solHtml=`<pre style="white-space:pre-wrap;max-height:600px;overflow-y:auto;margin-top:12px;padding:12px;background:#1a1b26;border-radius:8px;font-size:13px">${sol.replace(/</g,'&lt;')}</pre>`}$('resultArea').innerHTML=`<div class="result ${ok?'result-ok':'result-fail'}"><div class="result-header"><span class="result-icon">${ok?'✅':'⚠️'}</span><div><div class="result-title" style="color:${ok?'#69db7c':'#ff6b6b'}">${isPair?(s.mode||'Pair')+' '+s.status:isP?'Planning '+s.status:isDrf?'Deep Refactor '+s.status:s.status}</div><div class="result-sub">${sub}</div></div>${deepBtn}<button class="btn-copy" onclick="copyResult()">Copy</button></div>${solHtml}</div>`}
async function deleteThread(id){await fetch(`/api/delete/${id}`,{method:'DELETE'});if(cid===id){cid=null;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='flex';$('taskInput').value='';$('progressArea').style.display='none'}loadThreads()}
function newThread(){if(pt)clearInterval(pt);cid=null;lmc=0;run=false;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='flex';$('taskInput').value='';$('taskInput').focus();$('progressArea').style.display='none';$('btnRun').disabled=false;$('btnStop').style.display='none';loadThreads()}
function setMode(m){curMode=m;const modes=['auto','fast','standard','full','parallel'];const btnMap={auto:'modeAuto',fast:'modeFast',standard:'modeStandard',full:'modeFull',parallel:'modeParallel'};const gradients={auto:'linear-gradient(135deg,#bc8cff,#58a6ff)',fast:'linear-gradient(135deg,#3fb950,#2ea043)',standard:'linear-gradient(135deg,#d29922,#e3b341)',full:'linear-gradient(135deg,#f85149,#da3633)',parallel:'linear-gradient(135deg,#58a6ff,#388bfd)'};modes.forEach(md=>{const btn=$(btnMap[md]);if(btn){btn.style.background=md===m?gradients[md]:'#2a2a4a';btn.style.color=md===m?'#000':'#888';btn.style.border=md===m?'none':'1px solid #3a3a5a'}});const titles={auto:'New task',fast:'Fast mode',standard:'Standard mode',full:'Full mode',parallel:'Parallel mode'};const subs={auto:'task를 분석해서 최적 경로 자동 선택<br>코드 수정, 브레인스토밍, 문서 작성, PPT 생성, 아키텍처 설계 등 모든 작업',fast:'간단한 수정, 저위험 작업<br>빠른 1-pass 처리',standard:'중간 복잡도, pair gen + critic<br>일반적인 개발 작업에 최적',full:'고난도 작업, 풀체인 + aux critic<br>아키텍처 설계, 보안 감사, PPT/PDF 생성',parallel:'비판 없이 2~3 AI 병렬 생성<br>속도 최적화, 파트별 분할 작업'};const roles={auto:'<span style="color:#bc8cff">Classifier</span><span style="color:#58a6ff">Auto Router</span><span style="color:#69db7c">Best Engine</span>',fast:'<span style="color:#3fb950">Claude (1-pass)</span><span style="color:#69db7c">Fast Response</span>',standard:'<span style="color:#d29922">Claude (Gen)</span><span style="color:#ff6b6b">Critics</span><span style="color:#da77f2">Synth</span>',full:'<span style="color:#f85149">Multi-AI (Gen)</span><span style="color:#ff6b6b">Codex+Gemini+Aux (Critics)</span><span style="color:#da77f2">Opus (Synth)</span>',parallel:'<span style="color:#58a6ff">Claude</span><span style="color:#388bfd">Codex</span><span style="color:#69db7c">Gemini (opt)</span>'};$('emptyTitle').textContent=titles[m]||'New task';$('emptySub').innerHTML=subs[m]||'';$('btnRun').textContent='Run';$('autoOpts').style.display=m==='auto'?'flex':'none';$('parallelOpts').style.display=m==='parallel'?'flex':'none';$('fullOpts').style.display=m==='full'?'flex':'none';$('rolesInfo').innerHTML=roles[m]||''}
async function startRun(){const task=$('taskInput').value.trim();if(!task||run)return;run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';lmc=0;
  const body={task,mode:curMode,claude_model:$('modelSelect').value};
  if(curMode==='auto'){const sc=$('autoScope').value;const ri=$('autoRisk').value;const ar=$('autoArtifact').value;if(sc!=='auto')body.scope=sc;if(ri!=='auto')body.risk=ri;if(ar!=='none')body.artifact_type=ar}
  if(curMode==='parallel'){body.pair_mode='pair'+$('parallelParts').value;const od=$('outputDir').value.trim();if(od)body.output_dir=od}
  if(curMode==='full'){const ar=$('fullArtifact').value;if(ar!=='none')body.artifact_type=ar;body.audience=$('fullAudience').value;body.tone=$('fullTone').value}
  const r=await fetch('/api/horcrux/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();
  if(d.solution){run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';$('messages').innerHTML=`<div class="msg msg-generator"><div class="msg-header"><span class="role-tag role-generator">Solution</span><span style="font-size:10px;color:#555">(${d.mode} / ${d.internal_engine})</span></div><pre>${esc(d.solution)}</pre></div>`;$('resultArea').innerHTML=`<div class="result result-ok"><div class="result-header"><span class="result-icon">✅</span><div><div class="result-title" style="color:#69db7c">${d.status}</div><div class="result-sub">${d.mode} · ${d.internal_engine} · score: ${d.score||0}/10</div></div><button class="btn-copy" onclick="navigator.clipboard.writeText(document.querySelector('.msg pre').textContent)">Copy</button></div></div>`;loadThreads();return}
  cid=d.job_id;loadThreads();pt=setInterval(poll,2000)}
async function poll(){if(!cid)return;
  const r=await fetch(statusUrl(cid));if(!r.ok){clearInterval(pt);run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';return}const s=await r.json();
  const isP=(cid||'').startsWith('plan_');const isPair=(cid||'').startsWith('pair_');
  if(isPair){const numParts=s.mode==='pair3'?3:2;const done=s.parts_done||0;$('progressLabel').textContent=`${s.phase||'parallel_gen'} — ${done}/${numParts} parts done`;$('progressFill').style.width=Math.min((done/numParts)*100,100)+'%';$('progressScore').textContent=s.mode==='pair3'?'3-AI Parallel':'2-AI Parallel';$('progressScore').style.color='#69db7c'}else if(isP){const phaseIdx={starting:0,generating:1,synthesizing:2,critiquing:3,polishing:4,completed:4};const pi=phaseIdx[s.phase]||0;$('progressLabel').textContent=`Phase ${pi}/4 — ${s.phase_detail||s.phase||'...'}`;$('progressFill').style.width=Math.min((pi/4)*100,100)+'%';if(s.avg_score>0){$('progressScore').textContent=`Avg Critic: ${s.avg_score.toFixed(1)}/10`;$('progressScore').style.color=s.avg_score>=THRESHOLD?'#69db7c':'#ff6b6b'}}else{$('progressLabel').textContent=`Round ${s.round||0}/${MAX_ROUNDS} - ${s.phase||'...'}`;
  $('progressFill').style.width=Math.min(((s.round||0)/MAX_ROUNDS)*100,100)+"%";
  if(s.avg_score>0){$('progressScore').textContent=`Score: ${s.avg_score.toFixed(1)} / ${THRESHOLD}`;$('progressScore').style.color=s.avg_score>=THRESHOLD?'#69db7c':'#ff6b6b'}}
  if(s.status!=='running'){
    clearInterval(pt);run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';
    const fr=await fetch(resultUrl(cid));const full=await fr.json();
    renderAll(full);renderResult(full);loadThreads();
  } else if(s.message_count>lmc){
    const fr=await fetch(resultUrl(cid));const full=await fr.json();
    const c=$('messages');const msgs=full.messages||[];
    const isP2=(cid||'').startsWith('plan_');for(let i=lmc;i<msgs.length;i++){const m=msgs[i];if(!isP2&&m.role==='generator'){c.innerHTML+=`<div class="round-divider">Round ${Math.floor(i/3)+1}</div>`}if(isP2){const phaseMap={generator:'Phase 1: Generate',synthesizer:'Phase 2: Synthesize',critic:'Phase 3: Critique',final:'Phase 4: Final Polish'};const ph=phaseMap[m.role];if(ph&&!c.innerHTML.includes(ph)){c.innerHTML+=`<div class="round-divider">${ph}</div>`}}const ro=ROLES[m.role]||{name:m.role,cls:'generator'};const label=m.label||ro.name||m.role;const cls=ro.cls;let sh='';if(m.score!==undefined){const p=m.score>=THRESHOLD;sh=`<span class="score ${p?'score-pass':'score-fail'}">${Number(m.score).toFixed(1)}/10</span>`}const modelTag=m.model?` <span style="font-size:10px;color:#555">(${m.model})</span>`:'';c.innerHTML+=`<div class="msg msg-${cls}"><div class="msg-header"><span class="role-tag role-${cls}">${label}</span>${modelTag}${sh}</div><pre>${esc(m.content)}</pre></div>`}
    lmc=msgs.length;sb();
  }}
async function stopRun(){if(!cid)return;const isP=(cid||'').startsWith('plan_');const isPair=(cid||'').startsWith('pair_');const isSi=(cid||'').startsWith('si_');const isHrx=(cid||'').startsWith('hrx_')||(cid||'').startsWith('adp_');if(isP){await fetch(`/api/planning/stop/${cid}`,{method:'POST'})}else if(isPair){await fetch(`/api/pair/stop/${cid}`,{method:'POST'})}else if(isHrx){await fetch(`/api/horcrux/stop/${cid}`,{method:'POST'})}else{await fetch(`/api/stop/${cid}`,{method:'POST'})}}
async function deepDive(parentId){const parentFull=await(await fetch(`/api/result/${parentId}`)).json();const task=parentFull.task||'';const focusHint=prompt(`Deep Dive 포커스 힌트 (선택사항, Enter 스킵):\n현재 task: ${task.slice(0,80)}`)??'';const finalTask=focusHint.trim()?`${task}\n\n[Deep Dive 포커스]: ${focusHint}`:task;if(!finalTask)return;if(pt)clearInterval(pt);run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';lmc=0;$('taskInput').value=finalTask;const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({task:finalTask,threshold:THRESHOLD,max_rounds:MAX_ROUNDS,parent_debate_id:parentId})});const d=await r.json();cid=d.debate_id;loadThreads();pt=setInterval(poll,1500)}
function copyResult(){fetch(resultUrl(cid)).then(r=>r.json()).then(s=>{const isPair=(cid||'').startsWith('pair_');let text='';if(isPair){const parts=s.messages||[];text=parts.filter(m=>m.role!=='architect').map(m=>`// === ${m.role} (${m.model||'unknown'}) ===\n${m.content}`).join('\n\n')}else{text=s.final_solution||s.final_plan||''}navigator.clipboard.writeText(text);const b=document.querySelector('.btn-copy');if(b){b.textContent='Copied!';setTimeout(()=>b.textContent='Copy',1500)}})}
function sb(){$('content').scrollTop=$('content').scrollHeight}
function $(id){return document.getElementById(id)}
function esc(t){return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
setMode('auto');loadThreads();
</script>
</body>
</html>
"""

register_planning_v2_routes(app)


# ── Horcrux v8 Unified API routes ──

horcrux_states = {}

@app.route("/api/horcrux/classify", methods=["POST"])
def horcrux_classify():
    """분류 미리보기 — 실행하지 않고 어떤 모드/엔진이 선택될지 확인."""
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
    if not task:
        return jsonify({"error": "task required"}), 400
    try:
        from core.adaptive import classify_task_complexity, build_stage_plan
        mode_override = data.get("mode", "auto")
        if mode_override == "auto":
            mode_override = None
        result = classify_task_complexity(
            task_description=task,
            task_type=data.get("task_type", "code"),
            num_files_touched=data.get("num_files", 1),
            estimated_scope=data.get("scope", "medium"),
            risk_level=data.get("risk", "medium"),
            artifact_type=data.get("artifact_type", "none"),
            user_mode_override=mode_override,
        )
        d = result.to_dict()
        try:
            plan = build_stage_plan(
                recommended_mode=result.recommended_mode,
                task_type=data.get("task_type", "code"),
                artifact_type=data.get("artifact_type", "none"),
            )
            d["stages"] = plan.enabled_stages
        except Exception:
            d["stages"] = []
        return jsonify(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/horcrux/run", methods=["POST"])
def horcrux_run():
    """통합 실행 엔드포인트. classify → engine 결정 → 해당 엔진 호출."""
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
    if not task:
        return jsonify({"error": "task required"}), 400

    from core.adaptive import classify_task_complexity

    mode_param = data.get("mode", "auto")
    mode_override = None if mode_param == "auto" else mode_param

    claude_model_param = data.get("claude_model", "")

    classification = classify_task_complexity(
        task_description=task,
        task_type=data.get("task_type", "code"),
        num_files_touched=data.get("num_files", 1),
        estimated_scope=data.get("scope", "medium"),
        risk_level=data.get("risk", "medium"),
        artifact_type=data.get("artifact_type", "none"),
        user_mode_override=mode_override,
        claude_model=claude_model_param,
    )

    # Sonnet 보정: hard → full 승격, easy/medium → Opus 추천 경고
    from core.adaptive.classifier import apply_sonnet_compensation
    classification = apply_sonnet_compensation(
        result=classification,
        claude_model=claude_model_param,
        task_description=task,
        estimated_scope=data.get("scope", "medium"),
        risk_level=data.get("risk", "medium"),
    )

    engine = classification.internal_engine.value
    mode = classification.recommended_mode.value
    if mode == "full_horcrux":
        mode = "full"
    intent = classification.detected_intent.value

    # project_dir 추출 및 검증
    project_dir = data.get("project_dir", "")
    if project_dir:
        try:
            validate_project_dir(project_dir)
        except Exception as e:
            print(f"[WARN] project_dir validation failed: {e} — reading files anyway")
            # 검증 실패해도 경로가 존재하면 읽기 시도

    # ── 동기 엔진: adaptive_fast / adaptive_standard / adaptive_full ──
    if engine.startswith("adaptive_"):
        try:
            from adaptive_orchestrator import run_adaptive
            # map engine → mode_override for orchestrator
            orch_mode_map = {
                "adaptive_fast": "fast",
                "adaptive_standard": "standard",
                "adaptive_full": "full_horcrux",
            }
            # project_dir이 있으면 코드를 읽어서 task에 주입
            adaptive_task = task
            if project_dir:
                project_code = _read_project_files(project_dir, max_chars=25000)
                if project_code:
                    adaptive_task = (
                        f"{task}\n\n"
                        f"=== 분석 가드레일 ===\n"
                        f"- 아래 제공된 코드에서 직접 확인한 이슈만 보고할 것\n"
                        f"- 코드에서 확인 불가능한 추정은 [추정]으로 명시할 것\n"
                        f"- 각 이슈에 반드시 해당 파일명과 실제 코드 인용을 포함할 것\n"
                        f"- 코드를 직접 인용하지 못하면 이슈로 보고하지 말 것\n"
                        f"- severity는 confirmed(코드에서 확인됨) / suspected(패턴상 추정) 중 하나를 추가 표기할 것\n"
                        f"=== 프로젝트 코드 ({project_dir}) ===\n"
                        f"{project_code}\n"
                        f"=== 위 코드를 기반으로 분석하라 ==="
                    )

            result = run_adaptive(
                task=adaptive_task,
                mode_override=orch_mode_map.get(engine),
                task_type=data.get("task_type", "code"),
                num_files=data.get("num_files", 1),
                scope=data.get("scope", "medium"),
                risk=data.get("risk", "medium"),
                artifact_type=data.get("artifact_type", "none"),
                interactive=data.get("interactive", "batch"),
            )
            # 2단계 검증: project_dir이 있으면 언급된 파일을 다시 읽고 검증
            solution_text = result.get("final_solution", "")
            if project_dir and solution_text:
                print(f"  [VERIFY] 2단계 검증 시작...")
                solution_text = _verify_analysis(solution_text, project_dir)
                result["final_solution"] = solution_text
                print(f"  [VERIFY] 검증 완료 ({len(solution_text)} chars)")

            # BUG-2 fix: 동기 응답에도 job_id 생성 → check()로 조회 가능
            sync_id = "hrx_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            horcrux_states[sync_id] = {
                "id": sync_id, "task": task, "status": "completed",
                "phase": "completed",
                "round": result.get("rounds", 1),
                "messages": [
                    {"role": "generator", "content": solution_text,
                     "model": f"Claude ({engine})", "score": result.get("final_score", 0),
                     "ts": datetime.now().isoformat()},
                ] if solution_text else [],
                "avg_score": result.get("final_score", 0),
                "final_solution": solution_text,
                "created_at": datetime.now().isoformat(),
                "finished_at": datetime.now().isoformat(),
            }
            # 로그 파일로도 저장 (서버 재시작 후에도 스레드 유지)
            try:
                log_file = LOG_DIR / f"{sync_id}.json"
                with open(log_file, "w", encoding="utf-8") as f:
                    json.dump(horcrux_states[sync_id], f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return jsonify({
                "status": "converged" if result.get("converged") else "completed",
                "job_id": sync_id,
                "mode": mode,
                "internal_engine": engine,
                "score": result.get("final_score", 0),
                "rounds": result.get("rounds", 0),
                "solution": result.get("final_solution", ""),
                "routing": {
                    "source": classification.routing_source.value,
                    "confidence": classification.confidence,
                    "intent": intent,
                    "reason": classification.reason,
                },
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: planning_pipeline (직접 호출, self-HTTP 제거) ──
    if engine == "planning_pipeline":
        try:
            from planning_v2 import plannings, run_planning_harness
            planning_id = "plan_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:23]
            claude_model_resolved = {"opus": "claude-opus-4-6", "sonnet": "claude-sonnet-4-6"}.get(data.get("claude_model", ""), "")
            task_type = data.get("task_type", "brainstorm")
            artifact_type = data.get("artifact_type", "doc")
            audience = data.get("audience", "general")
            tone = data.get("tone", "professional")
            project_dir = data.get("project_dir", "")
            if project_dir:
                try:
                    validate_project_dir(project_dir)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400

            plannings[planning_id] = {
                "id": planning_id, "task": task, "task_type": task_type,
                "artifact_type": artifact_type, "status": "running",
                "phase": "starting", "phase_detail": "", "messages": [],
                "merged_plan": "", "final_plan": "", "final_solution": "",
                "artifact_spec": None, "avg_score": 0, "error": None,
                "abort": False, "claude_model": claude_model_resolved,
                "audience": audience, "purpose": task[:200], "tone": tone,
                "created_at": datetime.now().isoformat(), "finished_at": None,
            }
            t = threading.Thread(
                target=run_planning_harness,
                args=(planning_id, task, task_type, artifact_type, claude_model_resolved,
                      audience, task[:200], tone, 7.5, 3, project_dir),
                daemon=True,
            )
            t.start()
            return jsonify({
                "status": "running",
                "job_id": planning_id,
                "internal_engine": engine,
                "mode": mode,
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: pair_generation (직접 호출) ──
    if engine == "pair_generation":
        try:
            pair_mode = data.get("pair_mode", "pair2")
            pair_id = "pair_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:23]
            output_dir = data.get("output_dir", "")
            pairs[pair_id] = {
                "id": pair_id, "task": task, "mode": pair_mode,
                "status": "running", "phase": "splitting", "messages": [],
                "results": {}, "spec": "", "error": None,
                "output_dir": output_dir,
                "created_at": datetime.now().isoformat(), "finished_at": None,
            }
            t = threading.Thread(target=run_pair, args=(pair_id, task, pair_mode, "", None), daemon=True)
            t.start()
            return jsonify({
                "status": "running",
                "job_id": pair_id,
                "internal_engine": engine,
                "mode": "parallel",
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: self_improve (직접 호출) ──
    if engine == "self_improve":
        try:
            si_id = "si_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            iterations = data.get("iterations", 3)
            self_improves[si_id] = {
                "id": si_id, "task": task, "status": "running",
                "iteration": 0, "total_iterations": iterations,
                "final_score": 0, "final_solution": "",
                "created_at": datetime.now().isoformat(), "finished_at": None,
            }
            t = threading.Thread(target=run_self_improve, args=(si_id, task, iterations), daemon=True)
            t.start()
            return jsonify({
                "status": "running",
                "job_id": si_id,
                "internal_engine": engine,
                "mode": mode,
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: deep_refactor (직접 호출) ──
    if engine == "deep_refactor":
        try:
            project_dir = data.get("project_dir", "")
            if not project_dir:
                return jsonify({"error": "project_dir is required for deep_refactor mode"}), 400
            try:
                validate_project_dir(project_dir)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            drf_id = "drf_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:23]
            claude_model_resolved = data.get("claude_model", "sonnet")
            threshold = data.get("threshold", 7.5)
            max_rounds = data.get("max_rounds", 3)
            create_drf_state(drf_id, task, project_dir)
            t = threading.Thread(
                target=run_deep_refactor,
                args=(drf_id, task, project_dir, claude_model_resolved, threshold, max_rounds),
                daemon=True,
            )
            t.start()
            return jsonify({
                "status": "running",
                "job_id": drf_id,
                "internal_engine": engine,
                "mode": "deep_refactor",
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: debate_loop (직접 호출) ──
    if engine == "debate_loop":
        try:
            debate_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            claude_model_resolved = {"opus": "claude-opus-4-6", "sonnet": "claude-sonnet-4-6"}.get(data.get("claude_model", ""), "")
            max_rounds = data.get("max_rounds", 5)
            threshold = data.get("threshold", 8.0)
            project_dir = data.get("project_dir", "")
            if project_dir:
                try:
                    validate_project_dir(project_dir)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
            debates[debate_id] = {
                "id": debate_id, "task": task, "status": "running",
                "round": 0, "phase": "starting", "messages": [],
                "avg_score": 0, "final_solution": "",
                "threshold": threshold, "max_rounds": max_rounds,
                "claude_model": claude_model_resolved,
                "project_dir": project_dir,
                "created_at": datetime.now().isoformat(), "finished_at": None,
            }
            vision_url_param = data.get("vision_url", "")
            t = threading.Thread(target=run_debate, args=(debate_id, task, threshold, max_rounds, "", claude_model_resolved, vision_url_param), daemon=True)
            t.start()
            return jsonify({
                "status": "running",
                "job_id": debate_id,
                "internal_engine": engine,
                "mode": mode,
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": f"unknown engine: {engine}"}), 400


@app.route("/api/horcrux/status/<job_id>")
def horcrux_status(job_id):
    """통합 상태 확인 — job_id prefix 기반 라우팅."""
    # deep_refactor 상태는 별도 dict
    state = deep_refactors.get(job_id) if job_id.startswith("drf_") else None
    if not state:
        state = horcrux_states.get(job_id)
    if not state:
        log_file = LOG_DIR / f"{job_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            horcrux_states[job_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": state.get("id"), "status": state.get("status"),
        "phase": state.get("phase", ""), "task": state.get("task", ""),
        "message_count": len(state.get("messages", [])),
        "avg_score": state.get("avg_score", 0),
        "created_at": state.get("created_at"), "finished_at": state.get("finished_at"),
        "error": state.get("error"),
    })


@app.route("/api/horcrux/result/<job_id>")
def horcrux_result(job_id):
    state = deep_refactors.get(job_id) if job_id.startswith("drf_") else None
    if not state:
        state = horcrux_states.get(job_id)
    if not state:
        log_file = LOG_DIR / f"{job_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            horcrux_states[job_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify(state)


@app.route("/api/horcrux/stop/<job_id>", methods=["POST"])
def horcrux_stop(job_id):
    state = horcrux_states.get(job_id)
    if state:
        state["status"] = "aborted"
        state["finished_at"] = datetime.now().isoformat()
    # interactive session stop
    i_sess = interactive_sessions.get(job_id)
    if i_sess:
        from core.adaptive import SessionCommand, FeedbackAction
        i_sess.resume(SessionCommand(action=FeedbackAction.STOP))
    return jsonify({"ok": True})


# ── Interactive Session store ──
interactive_sessions = {}


@app.route("/api/horcrux/feedback", methods=["POST"])
def horcrux_feedback():
    """Interactive session에 피드백 주입 + 다음 라운드 재개."""
    data = request.json
    job_id = data.get("job_id", "")
    action = data.get("action", "continue")

    i_sess = interactive_sessions.get(job_id)
    if not i_sess:
        return jsonify({"error": f"interactive session not found: {job_id}"}), 404

    from core.adaptive import SessionCommand, FeedbackAction as FA

    action_map = {
        "continue": FA.CONTINUE, "feedback": FA.FEEDBACK,
        "focus": FA.FOCUS, "stop": FA.STOP, "rollback": FA.ROLLBACK,
    }
    fa = action_map.get(action, FA.CONTINUE)

    cmd = SessionCommand(
        action=fa,
        human_directive=data.get("human_directive", ""),
        focus_area=data.get("focus_area", ""),
        focus_depth=data.get("focus_depth", "deep"),
        rollback_to_round=data.get("rollback_to_round", 0),
        new_directive=data.get("new_directive", ""),
    )

    # rollback 시 irreversible 경고
    if fa == FA.ROLLBACK and i_sess.side_effects.has_irreversible_after(cmd.rollback_to_round):
        irr = i_sess.side_effects.irreversible_rounds_after(cmd.rollback_to_round)
        return jsonify({
            "status": "warning",
            "irreversible_warning": f"Rounds {irr} have irreversible side effects. Send again to confirm.",
            "irreversible_rounds": irr,
        })

    i_sess.resume(cmd)
    return jsonify({
        "status": i_sess.state.value,
        "message": f"action={action} applied",
        "next_round": i_sess.current_round + 1,
    })


@app.route("/api/horcrux/session/<job_id>")
def horcrux_session(job_id):
    """Interactive session 상세 상태."""
    i_sess = interactive_sessions.get(job_id)
    if not i_sess:
        return jsonify({"error": "not found"}), 404
    return jsonify(i_sess.to_dict())


# ── P2-002: Analytics routes → core/api/analytics_routes.py ──
from core.api.analytics_routes import analytics_bp
app.register_blueprint(analytics_bp)


if __name__ == "__main__":
    # Deep Refactor 의존성 주입
    inject_drf_callers(
        call_claude=call_claude, call_codex=call_codex, call_gemini=call_gemini,
        call_aux_critic_fn=_call_aux_critic, aux_endpoints=AUX_CRITIC_ENDPOINTS,
        extract_json_fn=extract_json, extract_score_fn=extract_score,
        log_dir=str(LOG_DIR),
    )

    print("\nHorcrux v8 - Adaptive Single Entry Point")
    print("  External: Auto / Fast / Standard / Full / Parallel / Deep Refactor")
    print("  Internal: adaptive_fast/standard/full, debate_loop, planning_pipeline, pair_generation, self_improve, deep_refactor")
    print(f"\n  API Key: {_HORCRUX_API_KEY[:8]}...  (set HORCRUX_API_KEY env or use X-API-Key header)")
    print("  Unified endpoint: /api/horcrux/run → classify → auto-route")
    print()
    # Aux API 키 감지 로그
    for name, _, env_key, model, _ in AUX_CRITIC_ENDPOINTS:
        val = os.environ.get(env_key, "")
        if val:
            print(f"  [AUX] {name} ({model}): KEY SET ({env_key}={val[:8]}...)")
        else:
            print(f"  [AUX] {name}: KEY MISSING ({env_key})")
    print(f"\n  http://localhost:5000")
    print(f"  Modes: Auto | Fast | Standard | Full | Parallel\n")
    # R06: localhost 전용 바인딩 (외부 접근 차단)
    _host = os.environ.get("HORCRUX_HOST", "127.0.0.1")
    app.run(host=_host, port=5000, debug=False)
