"""
core/router.py — Improvement #7: Task 라우팅 & 전문화

task 유형 자동 감지 → 최적 provider 조합 선택.
config.json으로 오버라이드 가능.
provider 성능 이력 기반 동적 라우팅.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .types import TaskType, RouteResult, ProviderStats


# ─── 키워드 사전 ───

_KEYWORDS: Dict[TaskType, set] = {
    TaskType.CODE: {
        "function","class","def","import","variable","bug","error","debug","refactor",
        "implement","api","endpoint","database","query","sql","html","css","javascript",
        "python","java","typescript","react","node","docker","git","compile","runtime",
        "exception","algorithm","array","tree","graph","hash","sort","recursion","async",
        "thread","rest","crud","orm","test","pytest","코드","함수","클래스","구현","버그",
        "디버그","리팩토링","알고리즘","데이터구조",
    },
    TaskType.MATH: {
        "calculate","compute","equation","formula","integral","derivative","matrix",
        "vector","probability","statistics","theorem","proof","solve","optimize",
        "linear","algebra","calculus","geometry","trigonometry","logarithm","polynomial",
        "계산","방정식","수식","적분","미분","행렬","확률","통계","증명","최적화",
    },
    TaskType.CREATIVE: {
        "write","story","poem","creative","fiction","narrative","character","plot",
        "dialogue","essay","blog","article","marketing","copy","slogan","brainstorm",
        "이야기","소설","시","창작","에세이","블로그","마케팅","카피","브레인스토밍",
    },
    TaskType.ANALYSIS: {
        "analyze","analyse","compare","evaluate","review","assess","examine","investigate",
        "research","study","report","insight","trend","pattern","explain","summarize",
        "분석","비교","평가","검토","조사","연구","보고서","인사이트","트렌드","요약",
    },
}

# 유형별 기본 provider 매핑
_DEFAULT_ROUTES: Dict[TaskType, Dict] = {
    TaskType.CODE:     {"generator": "claude", "critics": ["codex", "gemini"]},
    TaskType.MATH:     {"generator": "gemini", "critics": ["claude", "codex"]},
    TaskType.CREATIVE: {"generator": "codex",  "critics": ["claude", "gemini"]},
    TaskType.ANALYSIS: {"generator": "claude", "critics": ["gemini", "codex"]},
    TaskType.GENERAL:  {"generator": "claude", "critics": ["codex", "gemini"]},
}


def detect_task_type(text: str) -> Tuple[TaskType, float]:
    """키워드 기반 task 유형 감지. (task_type, confidence) 반환."""
    words = set(re.findall(r"\b[a-z가-힣]{2,}\b", text.lower()))
    scores: Dict[TaskType, int] = {}
    for t, kws in _KEYWORDS.items():
        scores[t] = len(words & kws)

    best = max(scores, key=lambda t: scores[t])
    total = sum(scores.values())
    conf  = scores[best] / total if total else 0.0

    if scores[best] == 0:
        return TaskType.GENERAL, 0.5
    return best, min(1.0, conf * 2)


# ─── 성능 이력 기반 동적 라우팅 ───

class ProviderHistory:
    """provider별 성능 이력 추적 (thread-safe)"""

    def __init__(self):
        self._stats: Dict[str, ProviderStats] = {}
        self._lock  = threading.Lock()

    def record(self, provider: str, success: bool, score: float,
               latency_ms: float, task_type: Optional[TaskType] = None):
        with self._lock:
            if provider not in self._stats:
                self._stats[provider] = ProviderStats(provider=provider)
            self._stats[provider].record(
                success=success, score=score,
                latency_ms=latency_ms, task_type=task_type
            )

    def best_for(self, task_type: TaskType, candidates: List[str]) -> str:
        """candidates 중 task_type에 가장 성능 좋은 provider 반환"""
        with self._lock:
            def score(p: str) -> float:
                s = self._stats.get(p)
                if not s or s.attempts < 3:
                    return 0.5  # 데이터 부족 → 중립
                # task_type 특화 점수 우선
                bt = s.by_task_type.get(task_type.value)
                if bt and bt["attempts"] >= 2:
                    return bt["total_score"] / bt["attempts"] / 10.0 * s.success_rate
                return s.avg_score / 10.0 * s.success_rate
            return max(candidates, key=score)

    def get_all(self) -> Dict[str, Dict]:
        with self._lock:
            return {p: s.to_dict() for p, s in self._stats.items()}


# ─── 메인 라우터 ───

class ProviderRouter:
    """
    task → (generator, critics) 최적 매핑.

    우선순위:
    1. config.json routing_overrides
    2. 성능 이력 기반 동적 선택 (3회 이상 데이터 있을 때)
    3. 유형별 기본값
    """

    def __init__(self, config_path: Optional[str | Path] = None):
        self.history = ProviderHistory()
        self._config_overrides: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        if config_path:
            self._load_config(Path(config_path))

    def _load_config(self, path: Path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            self._config_overrides = cfg.get("routing_overrides", {})
        except Exception as e:
            print(f"[Router] config load error: {e}")

    def route(self, task: str) -> RouteResult:
        task_type, confidence = detect_task_type(task)

        # 1. config 오버라이드
        override = self._config_overrides.get(task_type.value)
        if override:
            return RouteResult(
                task_type=task_type,
                generator=override.get("generator", "claude"),
                critics=override.get("critics", ["codex", "gemini"]),
                confidence=confidence,
                reason=f"config override ({task_type.value})",
                overridden=True,
            )

        defaults = _DEFAULT_ROUTES[task_type]
        gen_candidates = [defaults["generator"]] + [
            c for c in defaults["critics"] if c != defaults["generator"]
        ]
        crit_candidates = defaults["critics"]

        # 2. 이력 기반 동적 선택
        best_gen = self.history.best_for(task_type, gen_candidates)
        best_critics = [c for c in crit_candidates if c != best_gen]
        if not best_critics:
            best_critics = [c for c in ["claude", "codex", "gemini"] if c != best_gen][:2]

        return RouteResult(
            task_type=task_type,
            generator=best_gen,
            critics=best_critics,
            confidence=confidence,
            reason=f"auto ({task_type.value}, conf={confidence:.2f})",
        )

    def record_result(self, provider: str, success: bool, score: float,
                      latency_ms: float, task_type: Optional[TaskType] = None):
        self.history.record(provider, success, score, latency_ms, task_type)

    def stats(self) -> Dict:
        return self.history.get_all()


# typing import (파일 상단에 두면 순환 import 이슈 가능성 → 여기서 처리)
from typing import Tuple  # noqa: E402
