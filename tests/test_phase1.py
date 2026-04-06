"""
tests/test_phase1.py — Phase 1 테스트 (core/adaptive/ package API)

Happy Path + Failure/Edge Cases
"""

import time
import pytest
from unittest.mock import patch

from core.adaptive import (
    classify_task_complexity,
    ClassificationResult,
    build_stage_plan,
    should_continue_revision,
    generate_session_id,
    HorcruxMode,
    RoutingSource,
    run_with_timeout_budget,
    StageStatus,
    get_config,
    REVISION_HARD_CAP,
)
from core.adaptive.timeout_budget import TimeoutBudgetResult


# ═══════════════════════════════════════════════════════════
#  classify_task_complexity Tests
# ═══════════════════════════════════════════════════════════

class TestClassifyTaskComplexity:
    """Happy Path"""

    def test_simple_fix_routes_to_fast(self):
        result = classify_task_complexity(
            task_description="fix typo in README",
            task_type="code",
            num_files_touched=1,
            estimated_scope="small",
            risk_level="low",
        )
        assert result.recommended_mode == HorcruxMode.FAST
        assert result.confidence >= 0.8
        assert result.routing_source == RoutingSource.HEURISTIC

    def test_portfolio_ppt_routes_to_full_horcrux(self):
        result = classify_task_complexity(
            task_description="create portfolio presentation for job interview",
            task_type="artifact",
            num_files_touched=1,
            estimated_scope="large",
            risk_level="high",
            artifact_type="ppt",
        )
        assert result.recommended_mode == HorcruxMode.FULL
        assert result.confidence >= 0.75

    def test_feature_add_routes_to_full(self):
        """v8.3: standard 폐기, feature_add → full로 라우팅."""
        result = classify_task_complexity(
            task_description="add user authentication endpoint to the API",
            task_type="code",
            num_files_touched=3,
            estimated_scope="medium",
            risk_level="medium",
        )
        assert result.recommended_mode == HorcruxMode.FULL

    def test_architecture_refactor_routes_to_full(self):
        result = classify_task_complexity(
            task_description="refactor the entire architecture to microservices",
            task_type="code",
            num_files_touched=15,
            estimated_scope="large",
            risk_level="high",
        )
        assert result.recommended_mode == HorcruxMode.FULL

    def test_user_override_takes_priority(self):
        result = classify_task_complexity(
            task_description="fix typo",
            task_type="code",
            num_files_touched=1,
            estimated_scope="small",
            risk_level="low",
            user_mode_override="full",
        )
        assert result.recommended_mode == HorcruxMode.FULL
        assert result.routing_source == RoutingSource.OVERRIDE
        assert result.confidence == 1.0


class TestClassifyEdgeCases:

    def test_ambiguous_task_defaults_to_full(self):
        """v8.3: standard 폐기, 애매한 태스크도 full로 라우팅."""
        result = classify_task_complexity(
            task_description="do something with the thing",
            task_type="code",
            num_files_touched=3,
            estimated_scope="medium",
            risk_level="medium",
        )
        assert result.recommended_mode == HorcruxMode.FULL

    def test_conflicting_keywords_routes_full(self):
        """v8.3: standard 폐기, 충돌 키워드도 full로 라우팅."""
        result = classify_task_complexity(
            task_description="simple fix but also architecture refactor needed",
            task_type="code",
            num_files_touched=5,
            estimated_scope="medium",
        )
        assert result.recommended_mode == HorcruxMode.FULL

    def test_korean_keywords_work(self):
        result = classify_task_complexity(
            task_description="오타 수정해줘 간단하게",
            task_type="document",
            num_files_touched=1,
            estimated_scope="small",
            risk_level="low",
        )
        assert result.recommended_mode == HorcruxMode.FAST

    def test_high_risk_goes_full(self):
        result = classify_task_complexity(
            task_description="update the config",
            risk_level="high",
        )
        assert result.recommended_mode == HorcruxMode.FULL


# ═══════════════════════════════════════════════════════════
#  build_stage_plan Tests
# ═══════════════════════════════════════════════════════════

class TestBuildStagePlan:

    def test_fast_mode_stages(self):
        plan = build_stage_plan(HorcruxMode.FAST)
        assert "generator" in plan.enabled_stages
        assert "light_critic" in plan.enabled_stages
        assert "finalize" in plan.enabled_stages
        assert "pair_generation" not in plan.enabled_stages
        assert "core_critic" not in plan.enabled_stages

    def test_standard_mode_stages(self):
        plan = build_stage_plan(HorcruxMode.STANDARD)
        assert "pair_generation" in plan.enabled_stages
        assert "synth" in plan.enabled_stages
        assert "core_critic" in plan.enabled_stages
        assert "revision" in plan.enabled_stages

    def test_full_horcrux_mode_stages(self):
        plan = build_stage_plan(HorcruxMode.FULL)
        assert "pair_generation" in plan.enabled_stages
        assert "core_critic" in plan.enabled_stages
        assert "conditional_aux_critic" in plan.enabled_stages
        assert "convergence_check" in plan.enabled_stages


# ═══════════════════════════════════════════════════════════
#  should_continue_revision Tests
# ═══════════════════════════════════════════════════════════

class TestShouldContinueRevision:

    def test_revision_does_not_exceed_hard_cap(self):
        decision = should_continue_revision(
            current_round=REVISION_HARD_CAP,
            converged=False,
            blocking_issue_count=3,
            progress_delta=1.5,
            timeout_budget_remaining_ms=50000,
        )
        assert decision.should_continue is False
        assert "hard cap" in decision.reason.lower() or "cap" in decision.reason.lower()

    def test_continues_when_under_cap(self):
        decision = should_continue_revision(
            current_round=1,
            converged=False,
            blocking_issue_count=2,
            progress_delta=2.0,
            timeout_budget_remaining_ms=30000,
        )
        assert decision.should_continue is True

    def test_stops_on_convergence(self):
        decision = should_continue_revision(
            current_round=1,
            converged=True,
        )
        assert decision.should_continue is False

    def test_stops_on_same_blockers(self):
        decision = should_continue_revision(
            current_round=1,
            converged=False,
            progress_delta=0.5,
            timeout_budget_remaining_ms=50000,
            same_blockers_repeated=True,
        )
        assert decision.should_continue is False

    def test_stops_on_timeout_exhausted(self):
        decision = should_continue_revision(
            current_round=1,
            converged=False,
            progress_delta=1.0,
            timeout_budget_remaining_ms=0,
        )
        assert decision.should_continue is False


# ═══════════════════════════════════════════════════════════
#  run_with_timeout_budget Tests
# ═══════════════════════════════════════════════════════════

class TestRunWithTimeoutBudget:

    def test_partial_result_on_timeout(self):
        def fast():
            time.sleep(0.05)
            return "fast"

        def slow():
            time.sleep(5.0)
            return "slow"

        result = run_with_timeout_budget(
            stage_name="pair_generation",
            tasks=[("claude", fast), ("codex", slow)],
            timeout_budget_ms=500,
            mode="standard",
            session_id="test-001",
        )

        assert len(result.completed_results) >= 1
        assert "codex" in result.timed_out_results
        assert result.fallback_used is True

    def test_all_timeout_graceful(self):
        def slow():
            time.sleep(5.0)

        result = run_with_timeout_budget(
            stage_name="pair_generation",
            tasks=[("a", slow), ("b", slow)],
            timeout_budget_ms=200,
            mode="standard",
            session_id="test-002",
        )

        assert len(result.timed_out_results) == 2
        assert result.fallback_used is True

    def test_success_no_fallback(self):
        def ok():
            return "done"

        result = run_with_timeout_budget(
            stage_name="core_critic",
            tasks=[("a", ok)],
            timeout_budget_ms=5000,
            mode="standard",
            session_id="test-003",
        )

        assert len(result.completed_results) == 1
        assert result.fallback_used is False
        assert result.unresolved_flag is False


# ═══════════════════════════════════════════════════════════
#  Config & Session Tests
# ═══════════════════════════════════════════════════════════

class TestConfig:

    def test_config_loads(self):
        cfg = get_config()
        assert cfg.revision.hard_cap == 2
        assert cfg.routing.llm_fallback_threshold == 0.6

    def test_timeout_defaults_exist(self):
        cfg = get_config()
        assert cfg.timeouts.generator_ms > 0
        assert cfg.timeouts.core_critic_ms > 0


class TestSessionId:

    def test_uuid4_format(self):
        sid = generate_session_id()
        assert len(sid) == 36
        assert sid.count("-") == 4

    def test_unique(self):
        assert generate_session_id() != generate_session_id()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
