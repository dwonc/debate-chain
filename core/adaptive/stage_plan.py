"""
core/adaptive/stage_plan.py — Mode별 실행 Stage 구성

classify_task_complexity() 결과를 받아 실제 실행할 stage 목록을 결정한다.
Orchestrator는 이 plan에 따라 각 stage를 순차/병렬 실행.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from .classifier import HorcruxMode


# ─── Stage 이름 상수 ───

class Stage:
    GENERATOR           = "generator"
    PAIR_GENERATION     = "pair_generation"
    SYNTH               = "synth"
    LIGHT_CRITIC        = "light_critic"
    CORE_CRITIC         = "core_critic"
    AUX_CRITIC          = "conditional_aux_critic"
    CONVERGENCE_CHECK   = "convergence_check"
    REVISION            = "revision"
    REVISION_OPTIONAL   = "revision_optional"
    FINALIZE            = "finalize"


# ─── Mode별 기본 파이프라인 ───

_MODE_PIPELINES = {
    HorcruxMode.FAST: [
        Stage.GENERATOR,
        Stage.LIGHT_CRITIC,
        Stage.REVISION_OPTIONAL,
        Stage.FINALIZE,
    ],
    HorcruxMode.STANDARD: [
        Stage.PAIR_GENERATION,
        Stage.SYNTH,
        Stage.CORE_CRITIC,
        Stage.REVISION,
        Stage.FINALIZE,
    ],
    HorcruxMode.FULL: [
        Stage.PAIR_GENERATION,
        Stage.SYNTH,
        Stage.CORE_CRITIC,
        Stage.AUX_CRITIC,
        Stage.CONVERGENCE_CHECK,
        Stage.REVISION,
        Stage.FINALIZE,
    ],
}

# 전체 가능한 stage 목록
_ALL_STAGES = set()
for stages in _MODE_PIPELINES.values():
    _ALL_STAGES.update(stages)


@dataclass
class StagePlan:
    """실행 plan: 어떤 stage를 켜고 어떤 stage를 스킵하는지."""
    mode: HorcruxMode
    enabled_stages: List[str]
    skipped_stages: List[str]
    reasoning: str

    def has_stage(self, stage_name: str) -> bool:
        return stage_name in self.enabled_stages

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "enabled_stages": self.enabled_stages,
            "skipped_stages": self.skipped_stages,
            "reasoning": self.reasoning,
        }


def build_stage_plan(
    recommended_mode: HorcruxMode,
    task_type: str = "code",
    artifact_type: str = "none",
) -> StagePlan:
    """
    선택된 mode에 따라 실제 실행 stage를 구성한다.

    Args:
        recommended_mode: classify_task_complexity()가 결정한 모드
        task_type: code|document|artifact|analysis
        artifact_type: none|ppt|pdf|doc

    Returns:
        StagePlan
    """
    enabled = list(_MODE_PIPELINES.get(recommended_mode, _MODE_PIPELINES[HorcruxMode.STANDARD]))
    reasons = [f"base pipeline for {recommended_mode.value}"]

    # ── artifact 작업 특화 조정 ──
    if artifact_type != "none" and task_type == "artifact":
        # artifact는 synth 단계에서 content 재해석 금지
        # convergence check는 정보량/흐름/누락만 점검
        reasons.append(f"artifact_type={artifact_type}: content drift 방지 적용")

    # ── document 작업에서 aux critic 생략 (standard에는 원래 없음) ──
    if task_type == "document" and recommended_mode == HorcruxMode.FULL:
        # 문서 작업은 aux critic 효용이 낮으므로 선택적 제거 가능
        # 단, full_horcrux에서는 유지 (conditional이니까 나중에 should_run_aux에서 판단)
        reasons.append("document task: aux_critic은 conditional로 유지")

    # ── fast mode에서 single file + code일 때 synth 완전 제거 ──
    if recommended_mode == HorcruxMode.FAST and task_type == "code":
        if Stage.SYNTH in enabled:
            enabled.remove(Stage.SYNTH)
            reasons.append("fast+code: synth 제거")

    skipped = sorted(_ALL_STAGES - set(enabled))

    return StagePlan(
        mode=recommended_mode,
        enabled_stages=enabled,
        skipped_stages=skipped,
        reasoning=" | ".join(reasons),
    )
