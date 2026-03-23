"""
core/sse.py — Improvement #5: 실시간 SSE 스트리밍

Flask SSE 엔드포인트용 이벤트 버스.
- 라운드 진행상황 실시간 스트리밍
- AI 응답을 즉시 클라이언트에 전달
- polling 대체
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Iterator


class SSEBus:
    """
    pub-sub 이벤트 버스.
    각 job_id마다 subscriber queue를 관리.
    """

    def __init__(self, max_history: int = 100):
        self._subs: dict[str, list[queue.Queue]] = {}
        self._history: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._max_history = max_history

    def publish(self, job_id: str, event: str, data: dict):
        """이벤트 발행"""
        payload = {"event": event, "data": data, "ts": time.time()}
        with self._lock:
            # 히스토리 저장
            history = self._history.setdefault(job_id, [])
            history.append(payload)
            if len(history) > self._max_history:
                history.pop(0)
            # 구독자들에게 전달
            for q in self._subs.get(job_id, []):
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    def subscribe(self, job_id: str, replay_history: bool = True) -> "SSESubscription":
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.setdefault(job_id, []).append(q)
            history = list(self._history.get(job_id, []))

        if replay_history:
            for item in history:
                q.put_nowait(item)

        return SSESubscription(job_id=job_id, q=q, bus=self)

    def unsubscribe(self, job_id: str, q: queue.Queue):
        with self._lock:
            subs = self._subs.get(job_id, [])
            if q in subs:
                subs.remove(q)

    def get_history(self, job_id: str) -> list[dict]:
        with self._lock:
            return list(self._history.get(job_id, []))


class SSESubscription:
    def __init__(self, job_id: str, q: queue.Queue, bus: SSEBus):
        self.job_id = job_id
        self._q = q
        self._bus = bus
        self._closed = False

    def stream(self, timeout: float = 30.0) -> Iterator[str]:
        """Flask Response generator용 SSE 스트림"""
        try:
            while not self._closed:
                try:
                    payload = self._q.get(timeout=timeout)
                    yield _format_sse(payload["event"], payload["data"])
                    if payload["event"] in ("converged", "failed", "aborted"):
                        break
                except queue.Empty:
                    # keepalive
                    yield ": keepalive\n\n"
        finally:
            self._bus.unsubscribe(self.job_id, self._q)

    def close(self):
        self._closed = True


def _format_sse(event: str, data: dict) -> str:
    """SSE 포맷: event: xxx\\ndata: {...}\\n\\n"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ─── 싱글턴 ───
_bus: SSEBus | None = None

def get_bus() -> SSEBus:
    global _bus
    if _bus is None:
        _bus = SSEBus()
    return _bus


# ─── Flask 라우트 헬퍼 ───
def make_sse_response(job_id: str):
    """
    사용법:
        @app.route("/stream/<job_id>")
        def stream(job_id):
            return make_sse_response(job_id)
    """
    from flask import Response
    sub = get_bus().subscribe(job_id)
    return Response(
        sub.stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
