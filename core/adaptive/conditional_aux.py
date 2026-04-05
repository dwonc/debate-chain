"""
core/adaptive/conditional_aux.py — Phase 2: Conditional Aux Critic Usage

Aux critic을 항상 돌리지 않고 불확실하거나 중요할 때만 활성화.

Activation conditions:
  - core critic disagreement
  - score가 threshold 근처에서 애매
  - critical issue unresolved
  - full_horcrux mode
  - high-risk task

Skip conditions:
  - fast mode
  - low-risk change
  - clear convergence from core critics only
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class AuxDecision:
    """should_run_aux_critics()의 반환값."""
    run_aux: bool
    reason: str

    def to_dict(self) -> dict:
        return {"run_aux": self.run_aux, "reason": self.reason}


def should_run_aux_critics(
    mode: str,
    core_scores: Dict[str, float],
    critical_issue_count: int = 0,
    risk_level: str = "medium",
    threshold: float = 8.0,
) -> AuxDecision:
    """
    Aux critic 활성화 판정.

    Args:
        mode: "fast" | "standard" | "full_horcrux"
        core_scores: {"model_a": score, "model_b": score} — core critic 점수
        critical_issue_count: 미해결 critical issue 수
        risk_level: "low" | "medium" | "high"
        threshold: convergence threshold

    Returns:
        AuxDecision
    """
    # ── Skip conditions (우선순위 높음) ──

    # fast mode → 항상 skip
    if mode == "fast":
        return AuxDecision(run_aux=False, reason="fast mode — aux skip")

    # low-risk + score 높음 → skip
    scores = list(core_scores.values())
    avg_score = sum(scores) / len(scores) if scores else 0

    if risk_level == "low" and avg_score >= threshold:
        return AuxDecision(
            run_aux=False,
            reason=f"low risk + clear convergence (avg={avg_score:.1f} >= {threshold})",
        )

    # ── Activation conditions ──

    # full → 항상 활성화 (R10: full_horcrux → full 통합)
    if mode in ("full", "full_horcrux"):
        return AuxDecision(run_aux=True, reason="full mode — aux always active")

    # high-risk → 활성화
    if risk_level == "high":
        return AuxDecision(run_aux=True, reason=f"high-risk task — aux activated")

    # core critic disagreement (점수 차이 >= 2.0)
    if len(scores) >= 2:
        disagreement = max(scores) - min(scores)
        if disagreement >= 2.0:
            return AuxDecision(
                run_aux=True,
                reason=f"core critic disagreement: delta={disagreement:.1f} (>= 2.0)",
            )

    # threshold 근처에서 애매 (±1.0)
    if abs(avg_score - threshold) <= 1.0:
        return AuxDecision(
            run_aux=True,
            reason=f"score near threshold: avg={avg_score:.1f}, threshold={threshold} (±1.0 ambiguous zone)",
        )

    # critical issue unresolved
    if critical_issue_count > 0:
        return AuxDecision(
            run_aux=True,
            reason=f"{critical_issue_count} critical issue(s) unresolved",
        )

    # ── Default: standard mode에서 명확히 converge → skip ──
    if avg_score >= threshold + 0.5:
        return AuxDecision(
            run_aux=False,
            reason=f"clear convergence (avg={avg_score:.1f} > {threshold}+0.5) — aux skip",
        )

    # 기본: standard에서 점수가 낮으면 활성화
    return AuxDecision(
        run_aux=avg_score < threshold,
        reason=f"standard mode: avg={avg_score:.1f} {'< threshold — aux activated' if avg_score < threshold else '>= threshold — aux skip'}",
    )
