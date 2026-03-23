"""
core/async_worker.py — Improvement #4: Async Worker + 작업 큐 분리

threading 기반 워커 풀 (Celery 없이 즉시 사용 가능).
- 동시 다중 debate 실행
- 우선순위 큐
- 작업 취소 / 타임아웃
- job_store 연동
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Any, Optional

from .job_store import get_store, JobStatus


@dataclass(order=True)
class WorkItem:
    priority: int
    job_id: str = field(compare=False)
    fn: Callable = field(compare=False)
    args: tuple = field(default_factory=tuple, compare=False)
    kwargs: dict = field(default_factory=dict, compare=False)
    timeout: int = field(default=600, compare=False)
    cancelled: threading.Event = field(default_factory=threading.Event, compare=False)


class AsyncWorkerPool:
    """
    thread 기반 워커 풀.
    우선순위 큐 + job_store 상태 자동 업데이트.
    """

    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers
        self._queue: queue.PriorityQueue[WorkItem] = queue.PriorityQueue()
        self._workers: list[threading.Thread] = []
        self._active_jobs: dict[str, WorkItem] = {}
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._start_workers()

    def _start_workers(self):
        for i in range(self.max_workers):
            t = threading.Thread(target=self._worker_loop, name=f"debate-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)

    def _worker_loop(self):
        store = get_store()
        while not self._shutdown.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item.cancelled.is_set():
                self._queue.task_done()
                continue

            with self._lock:
                self._active_jobs[item.job_id] = item

            try:
                store.transition(item.job_id, JobStatus.RUNNING)
                result = item.fn(*item.args, **item.kwargs)
                store.transition(item.job_id, JobStatus.CONVERGED, result=result or {})
            except Exception as e:
                tb = traceback.format_exc()
                try:
                    store.transition(
                        item.job_id, JobStatus.FAILED,
                        error=f"{type(e).__name__}: {e}\n{tb[:1000]}"
                    )
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._active_jobs.pop(item.job_id, None)
                self._queue.task_done()

    def submit(
        self,
        job_id: str,
        fn: Callable,
        *args,
        priority: int = 5,
        timeout: int = 600,
        **kwargs,
    ) -> WorkItem:
        """작업 큐에 추가. priority 낮을수록 먼저 실행."""
        store = get_store()
        # job이 없으면 생성
        if store.get(job_id) is None:
            store.create(job_id, "generic", payload={"fn": fn.__name__})

        item = WorkItem(
            priority=priority,
            job_id=job_id,
            fn=fn,
            args=args,
            kwargs=kwargs,
            timeout=timeout,
        )
        self._queue.put(item)
        return item

    def cancel(self, job_id: str) -> bool:
        """실행 중이지 않은 작업 취소"""
        store = get_store()
        with self._lock:
            if job_id in self._active_jobs:
                return False  # 이미 실행 중이면 취소 불가
            # 큐에서 찾아서 cancelled 플래그
            for item in list(self._queue.queue):
                if item.job_id == job_id:
                    item.cancelled.set()
                    break
        try:
            store.transition(job_id, JobStatus.ABORTED)
            return True
        except Exception:
            return False

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_jobs)

    def queue_size(self) -> int:
        return self._queue.qsize()

    def shutdown(self, wait: bool = True):
        self._shutdown.set()
        if wait:
            for t in self._workers:
                t.join(timeout=5)


# ─── 싱글턴 ───
_pool: Optional[AsyncWorkerPool] = None

def get_pool(max_workers: int = 4) -> AsyncWorkerPool:
    global _pool
    if _pool is None:
        _pool = AsyncWorkerPool(max_workers)
    return _pool
