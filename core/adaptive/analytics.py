"""
core/adaptive/analytics.py — Phase 3: 운영 데이터 기반 튜닝 + 자동 최적화

5개 항목:
  1. timeout_auto_tuning — jsonl latency 로그에서 P50/P90/P99 산출 → timeout 자동 조정
  2. routing_heuristic_refinement — mode_usage, quality, latency로 heuristic 미세 조정
  3. llm_fallback_for_classification — confidence < 0.6 → lightweight LLM 호출
  4. critic_reliability_weighting — critic별 정확도 추적 → 동적 가중치
  5. mode_quality_latency_analytics — 모드별 품질/속도 대시보드 데이터
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import get_config


LOG_DIR = Path(__file__).parent.parent.parent / "logs"


def _guess_critic_model(text: str, model_hint: str = "") -> str:
    """critic의 모델명을 추정."""
    combined = (text + " " + model_hint).lower()
    if "gemini_fast" in combined or "flash-lite" in combined or "flash_lite" in combined:
        return "gemini_fast"
    if "gemini" in combined:
        return "gemini"
    if "claude" in combined:
        return "claude"
    if "codex" in combined or "gpt" in combined or "openai" in combined:
        return "codex"
    if "groq" in combined or "llama" in combined:
        return "groq"
    if "deepseek" in combined:
        return "deepseek"
    # verifier는 보통 gemini
    if "verifier" in combined:
        return "gemini"
    # critic default는 codex
    return "codex"


# ═══════════════════════════════════════════
# 1. Timeout Auto-Tuning
# ═══════════════════════════════════════════

@dataclass
class PercentileStats:
    """latency 백분위 통계."""
    count: int = 0
    p50: float = 0.0
    p90: float = 0.0
    p99: float = 0.0
    mean: float = 0.0
    recommended_timeout_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "count": self.count, "p50": round(self.p50),
            "p90": round(self.p90), "p99": round(self.p99),
            "mean": round(self.mean),
            "recommended_timeout_ms": self.recommended_timeout_ms,
        }


def _percentile(sorted_values: List[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = (pct / 100.0) * (len(sorted_values) - 1)
    low = int(math.floor(idx))
    high = min(low + 1, len(sorted_values) - 1)
    frac = idx - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def compute_latency_percentiles(
    log_path: Optional[Path] = None,
    stage_filter: Optional[str] = None,
    mode_filter: Optional[str] = None,
) -> Dict[str, PercentileStats]:
    """
    jsonl latency 로그에서 stage별 P50/P90/P99를 산출.

    Returns: {stage_name: PercentileStats}
    """
    path = log_path or get_config().logging.log_path
    if not path.exists():
        return {}

    stage_latencies: Dict[str, List[float]] = defaultdict(list)

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            stage = entry.get("stage_name", "")
            latency = entry.get("latency_ms", 0)
            status = entry.get("status", "")
            mode = entry.get("mode", "")

            if stage_filter and stage != stage_filter:
                continue
            if mode_filter and mode != mode_filter:
                continue
            if status in ("timed_out", "skipped"):
                continue
            if latency > 0:
                stage_latencies[stage].append(float(latency))

    results = {}
    for stage, values in stage_latencies.items():
        values.sort()
        n = len(values)
        if n == 0:
            continue
        p50 = _percentile(values, 50)
        p90 = _percentile(values, 90)
        p99 = _percentile(values, 99)
        mean = sum(values) / n
        # recommended: 1.5x~2x of P90, clamped
        recommended = int(p90 * 1.75)
        recommended = max(recommended, 5000)   # minimum 5s
        recommended = min(recommended, 300000)  # maximum 5min

        results[stage] = PercentileStats(
            count=n, p50=p50, p90=p90, p99=p99,
            mean=mean, recommended_timeout_ms=recommended,
        )

    return results


def auto_tune_timeouts(
    log_path: Optional[Path] = None,
    dry_run: bool = True,
) -> Dict[str, int]:
    """
    P90 기반으로 timeout 값을 자동 조정.
    dry_run=True면 추천값만 반환, False면 config에 실제 반영.

    Returns: {stage_name: recommended_timeout_ms}
    """
    stats = compute_latency_percentiles(log_path)
    recommendations = {}

    stage_to_config = {
        "generator": "generator_ms",
        "pair_generation": "generator_ms",
        "synth": "synth_ms",
        "core_critic": "core_critic_ms",
        "conditional_aux_critic": "aux_critic_ms",
        "light_critic": "light_critic_ms",
        "revision": "revision_ms",
    }

    for stage, pstats in stats.items():
        if pstats.count < 5:  # 최소 5개 데이터 포인트 필요
            continue
        config_key = stage_to_config.get(stage)
        if config_key:
            recommendations[config_key] = pstats.recommended_timeout_ms

    if not dry_run and recommendations:
        cfg = get_config()
        for key, value in recommendations.items():
            if hasattr(cfg.timeouts, key):
                setattr(cfg.timeouts, key, value)

    return recommendations


# ═══════════════════════════════════════════
# 2. Routing Heuristic Refinement
# ═══════════════════════════════════════════

@dataclass
class ModeUsageStats:
    """모드별 사용 통계."""
    mode: str
    usage_count: int = 0
    avg_score: float = 0.0
    avg_latency_ms: float = 0.0
    convergence_rate: float = 0.0

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "usage_count": self.usage_count,
            "avg_score": round(self.avg_score, 2),
            "avg_latency_ms": round(self.avg_latency_ms),
            "convergence_rate": round(self.convergence_rate, 3),
        }


def _infer_mode(data: dict, filename: str) -> str:
    """로그 데이터에서 모드를 추론."""
    mode = data.get("mode", "")
    if mode:
        return mode
    fid = data.get("id", filename)
    if isinstance(fid, str):
        if fid.startswith("pair_"):
            return "parallel"
        if fid.startswith("plan_"):
            return "planning"
        if fid.startswith("si_"):
            return "self_improve"
    if data.get("round") is not None or data.get("raw_steps"):
        status = data.get("status", "")
        if status == "converged" or data.get("avg_score", 0) >= 7.5:
            return "full"
        return "standard"
    return "unknown"


def compute_mode_usage_stats(log_dir: Optional[Path] = None) -> Dict[str, ModeUsageStats]:
    """모든 로그에서 모드별 사용/품질/속도 통계 산출."""
    d = log_dir or LOG_DIR
    mode_data: Dict[str, List[dict]] = defaultdict(list)

    for f in d.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            mode = _infer_mode(data, f.stem)
            mode_data[mode].append(data)
        except (json.JSONDecodeError, IOError):
            continue

    results = {}
    for mode, entries in mode_data.items():
        n = len(entries)
        scores = [e.get("final_score") or e.get("avg_score") or 0 for e in entries if (e.get("final_score") or e.get("avg_score"))]
        latencies = [e.get("total_latency_ms", 0) for e in entries if e.get("total_latency_ms")]
        converged = sum(1 for e in entries if e.get("converged") or e.get("status") == "converged")

        results[mode] = ModeUsageStats(
            mode=mode,
            usage_count=n,
            avg_score=sum(scores) / len(scores) if scores else 0,
            avg_latency_ms=sum(latencies) / len(latencies) if latencies else 0,
            convergence_rate=converged / n if n else 0,
        )

    return results


@dataclass
class HeuristicRefinement:
    """heuristic 미세 조정 추천."""
    suggestions: List[str] = field(default_factory=list)
    keyword_additions: Dict[str, List[str]] = field(default_factory=dict)
    keyword_removals: Dict[str, List[str]] = field(default_factory=dict)
    threshold_adjustments: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "suggestions": self.suggestions,
            "keyword_additions": self.keyword_additions,
            "keyword_removals": self.keyword_removals,
            "threshold_adjustments": self.threshold_adjustments,
        }


def suggest_heuristic_refinements(
    mode_stats: Optional[Dict[str, ModeUsageStats]] = None,
) -> HeuristicRefinement:
    """모드별 통계를 기반으로 heuristic 미세 조정 추천."""
    if mode_stats is None:
        mode_stats = compute_mode_usage_stats()

    refinement = HeuristicRefinement()

    fast = mode_stats.get("fast")
    standard = mode_stats.get("standard")
    full = mode_stats.get("full") or mode_stats.get("full_horcrux")

    # fast mode가 score 낮으면 → fast 기준 타이트하게
    if fast and fast.avg_score < 6.0 and fast.usage_count >= 3:
        refinement.suggestions.append(
            f"fast mode avg_score={fast.avg_score:.1f} (< 6.0) — fast 진입 기준을 더 엄격하게"
        )
        refinement.threshold_adjustments["fast_min_confidence"] = 0.90

    # full이 latency 과도하면 → standard로 더 많이 라우팅
    if full and full.avg_latency_ms > 120000 and standard:
        refinement.suggestions.append(
            f"full avg_latency={full.avg_latency_ms:.0f}ms (> 120s) — standard 라우팅 비중 확대 고려"
        )

    # standard convergence rate 높으면 → standard 충분
    if standard and standard.convergence_rate > 0.8:
        refinement.suggestions.append(
            f"standard convergence_rate={standard.convergence_rate:.1%} — standard가 충분히 효과적"
        )

    # fast usage가 0이면 → fast 키워드 확장 권장
    if not fast or fast.usage_count == 0:
        refinement.suggestions.append(
            "fast mode 사용 0건 — fast 키워드 범위를 확장하거나 scope 기준을 완화"
        )

    return refinement


# ═══════════════════════════════════════════
# 3. LLM Fallback for Classification
# ═══════════════════════════════════════════

LLM_CLASSIFY_PROMPT = """Classify this task into one of: fast, standard, full.

Rules:
- fast: trivial changes (typo fix, rename, lint, simple bug fix)
- standard: moderate changes (new feature, test, refactor 1-3 files)
- full: complex changes (architecture, multi-file refactor, security, production deploy)

Task: {task}
Type: {task_type}

Respond with ONLY one word: fast, standard, or full"""


def build_llm_classify_prompt(task: str, task_type: str = "code") -> str:
    """lightweight LLM 분류 프롬프트."""
    return LLM_CLASSIFY_PROMPT.format(
        task=task[:500],
        task_type=task_type,
    )


def parse_llm_classify_response(response_text: str) -> Tuple[str, float]:
    """LLM 응답에서 mode를 파싱. (mode, confidence)."""
    text = response_text.strip().lower()

    for mode in ("full", "full_horcrux", "standard", "fast"):
        if mode in text:
            resolved = "full" if mode == "full_horcrux" else mode
            confidence = 0.80 if text == mode else 0.70
            return resolved, confidence

    return "standard", 0.50


# ═══════════════════════════════════════════
# 4. Critic Reliability Weighting
# ═══════════════════════════════════════════

@dataclass
class CriticReliability:
    """critic별 신뢰도 점수."""
    model: str
    total_reviews: int = 0
    score_variance: float = 0.0
    avg_score_delta: float = 0.0  # critic score vs final score 차이
    reliability_score: float = 1.0  # 0.0~1.0
    recommended_weight: float = 1.0

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "total_reviews": self.total_reviews,
            "score_variance": round(self.score_variance, 3),
            "avg_score_delta": round(self.avg_score_delta, 3),
            "reliability_score": round(self.reliability_score, 3),
            "recommended_weight": round(self.recommended_weight, 3),
        }


def compute_critic_reliability(log_dir: Optional[Path] = None) -> Dict[str, CriticReliability]:
    """
    critic별 정확도/일관성을 추적하여 신뢰도 점수 산출.

    모든 로그 타입을 읽음:
    - *_result.json (adaptive): history[].score vs final_score
    - debate/regular .json: messages[role=critic/verifier].score vs avg_score
    - plan_*.json: messages[role=critic/verifier].score vs avg_score

    - critic score vs final_score 차이가 작을수록 reliable
    - score 분산이 작을수록 consistent
    - reliability_score = 1.0 - normalized(avg_delta + variance)
    """
    d = log_dir or LOG_DIR
    critic_data: Dict[str, List[Tuple[float, float]]] = defaultdict(list)

    for f in d.glob("*.json"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, IOError):
            continue

        # 방법 1: adaptive _result.json — history[].score vs final_score
        final_score = data.get("final_score", 0)
        if final_score and data.get("history"):
            for h in data["history"]:
                # per-critic scores가 있으면 개별 추적 (fast 모드 등)
                per_scores = h.get("critic_scores", {})
                if per_scores:
                    for cname, cscore in per_scores.items():
                        if cscore and cscore > 0:
                            critic_data[cname].append((cscore, final_score))
                else:
                    critic_text = h.get("critic", "")
                    score = h.get("score", 0)
                    if score > 0:
                        model = _guess_critic_model(critic_text, h.get("model", ""))
                        critic_data[model].append((score, final_score))
            continue

        # 방법 2: debate/plan/regular — messages[].score vs avg_score
        avg_score = data.get("avg_score", 0)
        if avg_score and data.get("messages"):
            for m in data["messages"]:
                role = m.get("role", "")
                score = m.get("score", 0)
                if score > 0 and role in ("critic", "verifier", "core_critic", "aux_critic"):
                    model = _guess_critic_model(
                        m.get("content", "")[:100],
                        m.get("model", role),
                    )
                    critic_data[model].append((score, avg_score))

    results = {}
    for model, pairs in critic_data.items():
        n = len(pairs)
        if n < 2:
            results[model] = CriticReliability(model=model, total_reviews=n)
            continue

        deltas = [abs(c - f) for c, f in pairs]
        scores = [c for c, _ in pairs]
        avg_delta = sum(deltas) / n
        mean_score = sum(scores) / n
        variance = sum((s - mean_score) ** 2 for s in scores) / n

        # reliability: lower delta + lower variance = higher reliability
        # normalize to 0~1 range
        delta_factor = min(avg_delta / 5.0, 1.0)  # delta 5.0 이상이면 최저
        var_factor = min(math.sqrt(variance) / 3.0, 1.0)  # std 3.0 이상이면 최저
        reliability = max(0.0, 1.0 - (delta_factor * 0.6 + var_factor * 0.4))

        # weight: 0.5 ~ 1.5 range
        weight = 0.5 + reliability

        results[model] = CriticReliability(
            model=model,
            total_reviews=n,
            score_variance=variance,
            avg_score_delta=avg_delta,
            reliability_score=reliability,
            recommended_weight=weight,
        )

    return results


# ═══════════════════════════════════════════
# 4.5 Scoring Weight Auto-Tuning
# ═══════════════════════════════════════════

def auto_tune_scoring_weights(
    min_reviews: int = 10,
    dry_run: bool = True,
    config_path: Optional[Path] = None,
) -> dict:
    """
    critic reliability 데이터 기반으로 Core/Aux 가중치 자동 튜닝.

    알고리즘:
      1. critic별 reliability_score (0~1) 와 recommended_weight (0.5~1.5) 수집
      2. Core critics (codex, gemini)의 평균 weight → core_raw
      3. Aux critics (Groq, DeepSeek, OpenRouter)의 평균 weight → aux_raw
      4. 정규화: core_weight = core_raw / (core_raw + aux_raw)
      5. 안전 범위: core_weight를 0.6~0.95로 clamp (aux가 아무리 좋아도 Core < 60% 안 됨)
      6. min_reviews 미달 시 기본값 0.8/0.2 유지

    Returns:
      {"core_weight": float, "aux_weight": float, "per_critic": {...}, "applied": bool, "reason": str}
    """
    reliability = compute_critic_reliability()

    # 데이터 충분한지 체크
    total_reviews = sum(r.total_reviews for r in reliability.values())
    if total_reviews < min_reviews:
        return {
            "core_weight": 0.8, "aux_weight": 0.2,
            "per_critic": {},
            "applied": False,
            "reason": f"insufficient data: {total_reviews}/{min_reviews} reviews",
        }

    # Core / Aux 분류
    core_names = {"codex", "gemini", "gemini_fast", "claude"}
    core_weights = []
    aux_weights = []
    per_critic = {}

    for model, rel in reliability.items():
        w = rel.recommended_weight
        per_critic[model] = {
            "reliability": rel.reliability_score,
            "weight": w,
            "reviews": rel.total_reviews,
            "avg_delta": rel.avg_score_delta,
        }
        model_lower = model.lower()
        if any(c in model_lower for c in core_names):
            core_weights.append(w)
        else:
            aux_weights.append(w)

    # 평균 weight 계산
    core_raw = sum(core_weights) / len(core_weights) if core_weights else 1.0
    aux_raw = sum(aux_weights) / len(aux_weights) if aux_weights else 0.5

    # 정규화 + clamp
    total = core_raw + aux_raw
    core_weight = core_raw / total if total > 0 else 0.8
    core_weight = max(0.6, min(0.95, core_weight))  # 안전 범위
    aux_weight = round(1.0 - core_weight, 3)
    core_weight = round(core_weight, 3)

    result = {
        "core_weight": core_weight,
        "aux_weight": aux_weight,
        "per_critic": per_critic,
        "applied": False,
        "reason": "dry_run" if dry_run else "applied",
    }

    # config.json에 저장
    if not dry_run:
        cfg_path = config_path or Path(__file__).parent.parent.parent / "config.json"
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if "scoring" not in cfg:
                cfg["scoring"] = {}
            cfg["scoring"]["core_weight"] = core_weight
            cfg["scoring"]["aux_weight"] = aux_weight
            cfg["scoring"]["per_critic_weights"] = per_critic
            cfg["scoring"]["last_tuned"] = datetime.now().isoformat()
            cfg["scoring"]["total_reviews"] = total_reviews
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            result["applied"] = True
            result["reason"] = f"applied to {cfg_path}"
        except Exception as e:
            result["reason"] = f"save failed: {e}"

    return result


# ═══════════════════════════════════════════
# 5. Mode Quality/Latency Analytics Dashboard
# ═══════════════════════════════════════════

@dataclass
class AnalyticsDashboard:
    """전체 analytics 데이터 (API/UI 렌더링용)."""
    mode_stats: Dict[str, dict] = field(default_factory=dict)
    timeout_stats: Dict[str, dict] = field(default_factory=dict)
    critic_reliability: Dict[str, dict] = field(default_factory=dict)
    heuristic_refinements: dict = field(default_factory=dict)
    timeout_recommendations: Dict[str, int] = field(default_factory=dict)
    total_sessions: int = 0
    total_stages_logged: int = 0

    def to_dict(self) -> dict:
        return {
            "mode_stats": self.mode_stats,
            "timeout_stats": self.timeout_stats,
            "critic_reliability": self.critic_reliability,
            "heuristic_refinements": self.heuristic_refinements,
            "timeout_recommendations": self.timeout_recommendations,
            "total_sessions": self.total_sessions,
            "total_stages_logged": self.total_stages_logged,
        }


def build_analytics_dashboard(log_dir: Optional[Path] = None) -> AnalyticsDashboard:
    """전체 analytics 대시보드 데이터 생성."""
    d = log_dir or LOG_DIR

    # Mode stats
    mode_stats_raw = compute_mode_usage_stats(d)
    mode_stats = {k: v.to_dict() for k, v in mode_stats_raw.items()}
    total_sessions = sum(v.usage_count for v in mode_stats_raw.values())

    # Timeout stats
    timeout_stats_raw = compute_latency_percentiles()
    timeout_stats = {k: v.to_dict() for k, v in timeout_stats_raw.items()}
    total_stages = sum(v.count for v in timeout_stats_raw.values())

    # Critic reliability
    critic_raw = compute_critic_reliability(d)
    critic_rel = {k: v.to_dict() for k, v in critic_raw.items()}

    # Heuristic refinements
    refinements = suggest_heuristic_refinements(mode_stats_raw)

    # Timeout recommendations
    timeout_recs = auto_tune_timeouts(dry_run=True)

    return AnalyticsDashboard(
        mode_stats=mode_stats,
        timeout_stats=timeout_stats,
        critic_reliability=critic_rel,
        heuristic_refinements=refinements.to_dict(),
        timeout_recommendations=timeout_recs,
        total_sessions=total_sessions,
        total_stages_logged=total_stages,
    )
