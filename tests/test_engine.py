"""
tests/test_engine.py — Phase 7: core/engine 모듈 단위 테스트

R44: critic, convergence, debate 로직 회귀 방지.
LLM 호출은 mock, 오케스트레이션 로직만 테스트.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


# ═══════════════════════════════════════════
# critic.py tests
# ═══════════════════════════════════════════

from core.engine.critic import (
    is_caller_error,
    extract_json,
    format_issues_compact,
    extract_score,
    normalize_critic_output,
    check_convergence_v2,
    build_revision_focus,
    build_compact_context_package,
)


class TestIsCallerError:
    def test_none_input(self):
        assert is_caller_error(None) is True

    def test_empty_string(self):
        assert is_caller_error("") is True

    def test_error_prefix(self):
        assert is_caller_error("[ERROR] Claude timeout") is True

    def test_valid_response(self):
        assert is_caller_error('{"scores": {"correctness": 8}}') is False

    def test_error_in_middle(self):
        assert is_caller_error('some text [ERROR] more text') is True


class TestExtractJson:
    def test_pure_json(self):
        result = extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_in_code_block(self):
        result = extract_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_error_input(self):
        assert extract_json("[ERROR] timeout") is None

    def test_empty_input(self):
        assert extract_json("") is None

    def test_none_input(self):
        assert extract_json(None) is None

    def test_nested_json(self):
        text = 'Here is the result: {"scores": {"correctness": 8}, "overall": 7.5}'
        result = extract_json(text)
        assert result is not None
        assert result["overall"] == 7.5

    def test_malformed_json(self):
        result = extract_json("not json at all")
        assert result is None


class TestExtractScore:
    def test_from_overall(self):
        data = {"overall": 8.5}
        assert extract_score(data, "") == 8.5

    def test_from_score_key(self):
        data = {"score": 7.0}
        assert extract_score(data, "") == 7.0

    def test_empty_data_returns_default(self):
        # extract_score returns 5.0 as default when no score found
        assert extract_score({}, "") == 5.0

    def test_none_data_returns_default(self):
        assert extract_score(None, "") == 5.0

    def test_score_from_raw_text(self):
        assert extract_score({}, "Overall: 7/10") == 7.0


class TestNormalizeCriticOutput:
    def test_already_normalized(self):
        raw = {
            "scores": {"correctness": 8, "completeness": 7},
            "overall": 7.5,
            "issues": [{"sev": "major", "desc": "test issue"}],
        }
        result = normalize_critic_output(raw, "TestCritic")
        # normalize_critic_output uses "score" key, not "overall"
        assert result["score"] == 7.5
        assert len(result["issues"]) == 1
        assert result["model"] == "TestCritic"

    def test_empty_input(self):
        result = normalize_critic_output({}, "TestCritic")
        assert result["score"] == 5.0  # default from extract_score
        assert result["issues"] == []

    def test_none_input(self):
        result = normalize_critic_output(None, "TestCritic")
        assert result["score"] == 5.0


class TestCheckConvergenceV2:
    def test_converged(self):
        data = {
            "overall": 8.5,
            "scores": {"correctness": 8, "completeness": 8, "security": 7, "performance": 7},
            "issues": [],
        }
        result = check_convergence_v2(data, threshold=8.0, min_per_dim=6.0)
        assert result["converged"] is True

    def test_below_threshold(self):
        data = {
            "overall": 6.0,
            "scores": {"correctness": 6},
            "issues": [],
        }
        result = check_convergence_v2(data, threshold=8.0)
        assert result["converged"] is False

    def test_critical_issue_blocks(self):
        data = {
            "overall": 9.0,
            "scores": {"correctness": 9},
            "issues": [{"sev": "critical", "desc": "SQL injection"}],
        }
        result = check_convergence_v2(data, threshold=8.0)
        assert result["converged"] is False

    def test_dim_below_minimum(self):
        data = {
            "overall": 8.5,
            "scores": {"correctness": 9, "security": 4},
            "issues": [],
        }
        result = check_convergence_v2(data, threshold=8.0, min_per_dim=6.0)
        assert result["converged"] is False


class TestBuildRevisionFocus:
    def test_basic(self):
        diagnostics = {
            "blocking_issues": [{"desc": "fix this", "dimension": "security"}],
            "weak_dimensions": ["security"],
        }
        critic_merged = {"issues": [{"sev": "critical", "desc": "fix this"}]}
        result = build_revision_focus(diagnostics, critic_merged)
        # Returns dict with revision instructions
        assert isinstance(result, dict)
        assert "blocking_issues" in result


class TestBuildCompactContext:
    def test_basic(self):
        result = build_compact_context_package(
            "solution summary", {"issues": []}, {"blocking_issues": []}, None
        )
        # Returns dict with context package
        assert isinstance(result, dict)


# ═══════════════════════════════════════════
# prompts.py tests
# ═══════════════════════════════════════════

from core.prompts import (
    GENERATOR_PROMPT, CRITIC_PROMPT, SYNTHESIZER_PROMPT,
    SPLIT_PROMPT, PART_PROMPT, SELF_IMPROVE_PROMPT,
)


class TestPrompts:
    def test_generator_has_task_placeholder(self):
        assert "{task}" in GENERATOR_PROMPT

    def test_critic_has_placeholders(self):
        assert "{task}" in CRITIC_PROMPT
        assert "{solution}" in CRITIC_PROMPT

    def test_synthesizer_has_placeholders(self):
        assert "{task}" in SYNTHESIZER_PROMPT
        assert "{solution}" in SYNTHESIZER_PROMPT

    def test_split_has_placeholders(self):
        assert "{task}" in SPLIT_PROMPT
        assert "{num_parts}" in SPLIT_PROMPT

    def test_self_improve_has_placeholders(self):
        assert "{prev}" in SELF_IMPROVE_PROMPT
        assert "{task}" in SELF_IMPROVE_PROMPT

    def test_generator_format_works(self):
        result = GENERATOR_PROMPT.format(task="test task")
        assert "test task" in result

    def test_critic_format_works(self):
        result = CRITIC_PROMPT.format(task="t", solution="s", previously_fixed="none")
        assert "t" in result


# ═══════════════════════════════════════════
# types.py error hierarchy tests
# ═══════════════════════════════════════════

from core.types import (
    HorcruxError, CallerError, CallerTimeoutError,
    QuotaExhaustedError, ModelUnavailableError, ParseError,
    InputError, PersistenceError, SecurityError,
)


class TestErrorHierarchy:
    def test_caller_error_is_horcrux_error(self):
        assert issubclass(CallerError, HorcruxError)

    def test_timeout_is_caller_error(self):
        assert issubclass(CallerTimeoutError, CallerError)

    def test_quota_is_caller_error(self):
        assert issubclass(QuotaExhaustedError, CallerError)

    def test_caller_error_has_provider(self):
        e = CallerError("Claude", "test error")
        assert e.provider == "Claude"
        assert "Claude" in str(e)

    def test_input_error_is_horcrux_error(self):
        assert issubclass(InputError, HorcruxError)

    def test_security_error_is_horcrux_error(self):
        assert issubclass(SecurityError, HorcruxError)


# ═══════════════════════════════════════════
# security.py path validation tests
# ═══════════════════════════════════════════

from core.security import validate_project_dir


class TestValidateProjectDir:
    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            validate_project_dir("")

    def test_nonexistent_raises(self):
        with pytest.raises(ValueError, match="does not exist"):
            validate_project_dir("C:\\nonexistent_path_12345")

    def test_valid_dir_passes(self, tmp_path):
        # tmp_path is a real directory
        result = validate_project_dir(str(tmp_path))
        assert result.is_dir()


# ═══════════════════════════════════════════
# classifier mode normalization tests
# ═══════════════════════════════════════════

from core.adaptive.classifier import normalize_mode, HorcruxMode


class TestNormalizeMode:
    def test_full_horcrux_alias(self):
        assert normalize_mode("full_horcrux") == "full"

    def test_full_stays(self):
        assert normalize_mode("full") == "full"

    def test_fast_stays(self):
        assert normalize_mode("fast") == "fast"

    def test_case_insensitive(self):
        assert normalize_mode("FULL_HORCRUX") == "full"

    def test_no_full_horcrux_in_enum(self):
        values = [m.value for m in HorcruxMode]
        assert "full_horcrux" not in values
        assert "full" in values
