"""
core/adaptive/ — Adaptive Horcrux Phase 1

난이도 기반 체인 깊이 조절, timeout budget, revision hard cap.

패키지 구조:
  classifier.py    — heuristic-first task routing
  config.py        — 설정 (timeout, feature flag, threshold)
  stage_plan.py    — mode별 stage 구성
  revision_gate.py — revision hard cap + 중단 판정
  timeout_budget.py — timeout budget + latency logging
"""

from .classifier import (
    classify_task_complexity,
    ClassificationResult,
    HorcruxMode,
    InternalEngine,
    DetectedIntent,
    RoutingSource,
)
from .config import (
    get_config,
    reload_config,
    AdaptiveHorcruxConfig,
    TimeoutConfig,
    RoutingConfig,
    RevisionConfig,
)
from .stage_plan import (
    build_stage_plan,
    StagePlan,
    Stage,
)
from .revision_gate import (
    should_continue_revision,
    RevisionDecision,
)
from .timeout_budget import (
    run_with_timeout_budget,
    log_stage_latency,
    generate_session_id,
    StageStatus,
    StageResult,
    TimeoutBudgetResult,
)
from .compact_memory import (
    CompactMemory,
    WorkingMemory,
    DecisionMemory,
    ResultSummaryMemory,
    RoundCheckpoint,
    DeltaPrompt,
    MEMORY_POLICY,
)
from .writer_lock import (
    WriterLock,
    AgentRole,
    Permission,
    ROLE_PERMISSIONS,
)
from .patch_format import (
    PatchHunk,
    FilePatch,
    PatchSet,
    parse_patch_from_llm_output,
    merge_patch_sets,
    PATCH_PROPOSAL_PROMPT_SUFFIX,
)
from .conditional_aux import (
    should_run_aux_critics,
    AuxDecision,
)
from .artifact_spec import (
    ArtifactSpec,
    SlideSpec,
    DocSection,
    build_artifact_spec_prompt,
    build_artifact_critic_prompt,
    ARTIFACT_CRITIC_PROMPT,
)
from .fallback_chain import (
    execute_fallback_chain,
    FallbackAction,
    FallbackResult,
    FallbackContext,
    FALLBACK_CHAINS,
)
from .interactive import (
    InteractiveSession,
    SessionConfig,
    SessionState,
    SessionCommand,
    AutoPauseConfig,
    AutoPauseEvaluator,
    FeedbackAction,
    RoundResult,
    CheckpointStore,
    Checkpoint,
    SideEffectJournal,
    SideEffectEntry,
    SideEffectType,
    DirectiveInjector,
    HumanDirective,
    DirectivePriority,
    FocusConstraint,
    PausePolicy,
    InterruptionPoint,
    ExecutionMode,
    VALID_TRANSITIONS,
    HorcruxInteractiveError,
    InvalidStateTransition,
    IrreversibleSideEffectError,
)
from .context_loader import (
    ProjectContext,
)
from .analytics import (
    compute_latency_percentiles,
    auto_tune_timeouts,
    compute_mode_usage_stats,
    suggest_heuristic_refinements,
    compute_critic_reliability,
    build_analytics_dashboard,
    AnalyticsDashboard,
    PercentileStats,
    ModeUsageStats,
    CriticReliability,
    build_llm_classify_prompt,
    parse_llm_classify_response,
)

# revision hard cap (config에서도 읽지만 편의상 상수 export)
REVISION_HARD_CAP = get_config().revision.hard_cap

__all__ = [
    # classifier
    "classify_task_complexity",
    "ClassificationResult",
    "HorcruxMode",
    "InternalEngine",
    "DetectedIntent",
    "RoutingSource",
    # config
    "get_config",
    "reload_config",
    "AdaptiveHorcruxConfig",
    "TimeoutConfig",
    "RoutingConfig",
    "RevisionConfig",
    # stage_plan
    "build_stage_plan",
    "StagePlan",
    "Stage",
    # revision_gate
    "should_continue_revision",
    "RevisionDecision",
    # timeout_budget
    "run_with_timeout_budget",
    "log_stage_latency",
    "generate_session_id",
    "StageStatus",
    "StageResult",
    "TimeoutBudgetResult",
    # Phase 1.5: compact_memory
    "CompactMemory",
    "WorkingMemory",
    "DecisionMemory",
    "ResultSummaryMemory",
    "RoundCheckpoint",
    "DeltaPrompt",
    "MEMORY_POLICY",
    # Phase 2: writer_lock
    "WriterLock",
    "AgentRole",
    "Permission",
    "ROLE_PERMISSIONS",
    # Phase 2: patch_format
    "PatchHunk",
    "FilePatch",
    "PatchSet",
    "parse_patch_from_llm_output",
    "merge_patch_sets",
    "PATCH_PROPOSAL_PROMPT_SUFFIX",
    # Phase 2: conditional_aux
    "should_run_aux_critics",
    "AuxDecision",
    # Phase 2: artifact_spec
    "ArtifactSpec",
    "SlideSpec",
    "DocSection",
    "build_artifact_spec_prompt",
    "build_artifact_critic_prompt",
    "ARTIFACT_CRITIC_PROMPT",
    # Phase 2: fallback_chain
    "execute_fallback_chain",
    "FallbackAction",
    "FallbackResult",
    "FallbackContext",
    "FALLBACK_CHAINS",
    # Phase 3: analytics
    "compute_latency_percentiles",
    "auto_tune_timeouts",
    "compute_mode_usage_stats",
    "suggest_heuristic_refinements",
    "compute_critic_reliability",
    "build_analytics_dashboard",
    "AnalyticsDashboard",
    "PercentileStats",
    "ModeUsageStats",
    "CriticReliability",
    "build_llm_classify_prompt",
    "parse_llm_classify_response",
    # interactive v3.0
    "InteractiveSession",
    "SessionConfig",
    "SessionState",
    "SessionCommand",
    "AutoPauseConfig",
    "AutoPauseEvaluator",
    "FeedbackAction",
    "RoundResult",
    "CheckpointStore",
    "Checkpoint",
    "SideEffectJournal",
    "SideEffectEntry",
    "SideEffectType",
    "DirectiveInjector",
    "HumanDirective",
    "DirectivePriority",
    "FocusConstraint",
    "PausePolicy",
    "InterruptionPoint",
    "ExecutionMode",
    "VALID_TRANSITIONS",
    "HorcruxInteractiveError",
    "InvalidStateTransition",
    "IrreversibleSideEffectError",
    # constants
    "REVISION_HARD_CAP",
]
