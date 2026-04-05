"""core/engine — R16: Domain engine modules extracted from server.py."""

from .critic import (
    is_caller_error,
    extract_json,
    format_issues_compact,
    extract_score,
    check_convergence,
    normalize_critic_output,
    check_convergence_v2,
    build_revision_focus,
    build_compact_context_package,
    extract_debate_artifact,
)

from .debate import run_multi_critic, run_debate, _maybe_auto_tune_scoring

__all__ = [
    "is_caller_error", "extract_json", "format_issues_compact", "extract_score",
    "check_convergence", "normalize_critic_output", "check_convergence_v2",
    "build_revision_focus", "build_compact_context_package", "extract_debate_artifact",
    "run_multi_critic", "run_debate", "_maybe_auto_tune_scoring",
]
