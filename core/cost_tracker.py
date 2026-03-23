"""
core/cost_tracker.py — Improvement #9: Cost & Rate Limit 관리

- 토큰 사용량 / 비용 per-job 기록
- 예산 한도 설정 + 초과 시 경고/차단
- rate limit 자동 백오프 (exponential)
- provider별 quota 소진 추적
"""

from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ─── 가격표 (USD per 1M tokens) ─ 2025년 기준 대략치
PRICE_TABLE: dict[str, dict[str, float]] = {
    "claude-opus-4-5":      {"input": 15.0,  "output": 75.0},
    "claude-sonnet-4-5":    {"input": 3.0,   "output": 15.0},
    "gpt-4o":               {"input": 5.0,   "output": 15.0},
    "gpt-4o-mini":          {"input": 0.15,  "output": 0.60},
    "gemini-2.5-flash":     {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash":     {"input": 0.075, "output": 0.30},
}

DEFAULT_BUDGET_USD = 5.0   # 기본 세션 예산


@dataclass
class UsageRecord:
    job_id: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    ts: str = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat()
        if not self.cost_usd and self.model in PRICE_TABLE:
            p = PRICE_TABLE[self.model]
            self.cost_usd = (
                self.tokens_in  / 1_000_000 * p["input"] +
                self.tokens_out / 1_000_000 * p["output"]
            )


class CostTracker:
    """
    thread-safe 비용 추적기.
    JSON 파일로 영속 저장.
    """

    def __init__(
        self,
        log_path: str | Path = "cost_log.jsonl",
        budget_usd: float = DEFAULT_BUDGET_USD,
    ):
        self.log_path = Path(log_path)
        self.budget_usd = budget_usd
        self._lock = threading.Lock()
        self._session_cost = 0.0
        self._records: list[UsageRecord] = []
        self._load_session()

    def _load_session(self):
        """당일 비용 로드"""
        today = datetime.now(timezone.utc).date().isoformat()
        if not self.log_path.exists():
            return
        try:
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("ts", "").startswith(today):
                    self._session_cost += r.get("cost_usd", 0)
        except Exception:
            pass

    def record(self, rec: UsageRecord) -> bool:
        """
        사용량 기록. 예산 초과 시 False 반환.
        """
        with self._lock:
            self._session_cost += rec.cost_usd
            self._records.append(rec)
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[CostTracker] write error: {e}")

        over_budget = self._session_cost > self.budget_usd
        if over_budget:
            print(f"[CostTracker] ⚠️  Budget exceeded: ${self._session_cost:.4f} / ${self.budget_usd}")
        return not over_budget

    def check_budget(self) -> tuple[float, float, bool]:
        """(used_usd, budget_usd, is_ok)"""
        with self._lock:
            return self._session_cost, self.budget_usd, self._session_cost <= self.budget_usd

    def summary(self) -> dict:
        with self._lock:
            by_provider: dict[str, dict] = {}
            for r in self._records:
                p = by_provider.setdefault(r.provider, {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0})
                p["tokens_in"]  += r.tokens_in
                p["tokens_out"] += r.tokens_out
                p["cost_usd"]   += r.cost_usd
                p["calls"]      += 1
            return {
                "session_cost_usd": round(self._session_cost, 6),
                "budget_usd": self.budget_usd,
                "remaining_usd": round(max(0, self.budget_usd - self._session_cost), 6),
                "by_provider": by_provider,
                "total_calls": len(self._records),
            }


# ─── Rate Limit 백오프 ───

class RateLimiter:
    """
    provider별 rate limit 자동 백오프.
    429 에러 발생 시 지수 백오프 후 재시도.
    """

    def __init__(self, base_delay: float = 2.0, max_delay: float = 60.0, max_retries: int = 4):
        self.base_delay = base_delay
        self.max_delay  = max_delay
        self.max_retries = max_retries
        self._backoff: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait_if_needed(self, provider: str):
        """이전 rate limit 후 대기 시간 소진"""
        with self._lock:
            until = self._backoff.get(provider, 0)
        remaining = until - time.monotonic()
        if remaining > 0:
            print(f"[RateLimit] {provider}: waiting {remaining:.1f}s")
            time.sleep(remaining)

    def on_rate_limit(self, provider: str, attempt: int):
        """429 수신 시 호출 → 다음 호출까지 백오프 설정"""
        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        with self._lock:
            self._backoff[provider] = time.monotonic() + delay
        print(f"[RateLimit] {provider}: backoff {delay:.1f}s (attempt {attempt})")
        time.sleep(delay)

    def on_success(self, provider: str):
        with self._lock:
            self._backoff.pop(provider, None)

    def call_with_retry(self, provider: str, fn, *args, **kwargs):
        """rate limit 자동 재시도 래퍼"""
        self.wait_if_needed(provider)
        last_err = None
        for attempt in range(self.max_retries):
            try:
                result = fn(*args, **kwargs)
                self.on_success(provider)
                return result
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if "429" in err_str or "rate" in err_str or "quota" in err_str:
                    self.on_rate_limit(provider, attempt)
                else:
                    raise
        raise RuntimeError(f"[RateLimit] {provider}: max retries exceeded") from last_err


# ─── 싱글턴 ───
_tracker: Optional[CostTracker] = None
_limiter: Optional[RateLimiter] = None

def get_tracker() -> CostTracker:
    global _tracker
    if _tracker is None:
        log_path = Path(__file__).parent.parent / "logs" / "cost_log.jsonl"
        log_path.parent.mkdir(exist_ok=True)
        budget = float(__import__("os").environ.get("DEBATE_BUDGET_USD", DEFAULT_BUDGET_USD))
        _tracker = CostTracker(log_path, budget)
    return _tracker

def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
