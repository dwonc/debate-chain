"""
core/__init__.py — Horcrux Core Modules

개선사항 #1~#10 통합 패키지 + Phase 1 Adaptive Horcrux.
"""

from .security     import redact, sanitize_prompt, run_cli_stdin, load_secret
from .job_store    import SQLiteJobStore, JobStatus, JobRecord, get_store
from .provider     import (
    ProviderBackend, ProviderResponse,
    ClaudeCLIBackend, CodexCLIBackend,
    make_core_pair, make_auxiliary, invoke_parallel,
)
from .async_worker import AsyncWorkerPool, get_pool
from .sse          import SSEBus, SSESubscription, get_bus, make_sse_response
from .cost_tracker import CostTracker, RateLimiter, get_tracker, get_limiter
# convergence — removed (R34: dead code)
from .types        import TaskType, RouteResult, ProviderStats, ToolResult
from .router       import ProviderRouter, detect_task_type
from .tools        import web_search, code_exec, file_read, inject_tools

# Phase 1 — Adaptive Horcrux (from core/adaptive/ package)
from .adaptive import (
    HorcruxMode, RoutingSource,
    ClassificationResult, StagePlan, RevisionDecision,
    classify_task_complexity, build_stage_plan,
    should_continue_revision, generate_session_id,
    run_with_timeout_budget, log_stage_latency,
    StageStatus, StageResult, TimeoutBudgetResult, Stage,
    get_config, reload_config, REVISION_HARD_CAP,
)

__all__ = [
    # security
    "redact", "sanitize_prompt", "run_cli_stdin", "load_secret",
    # job_store
    "SQLiteJobStore", "JobStatus", "JobRecord", "get_store",
    # provider
    "ProviderBackend", "ProviderResponse",
    "ClaudeCLIBackend", "CodexCLIBackend",
    "make_core_pair", "make_auxiliary", "invoke_parallel",
    # async
    "AsyncWorkerPool", "get_pool",
    # sse
    "SSEBus", "SSESubscription", "get_bus", "make_sse_response",
    # cost
    "CostTracker", "RateLimiter", "get_tracker", "get_limiter",
    # types
    "TaskType", "RouteResult", "ProviderStats", "ToolResult",
    # router
    "ProviderRouter", "detect_task_type",
    # tools
    "web_search", "code_exec", "file_read", "inject_tools",
    # adaptive (Phase 1)
    "HorcruxMode", "RoutingSource",
    "ClassificationResult", "StagePlan", "RevisionDecision",
    "classify_task_complexity", "build_stage_plan",
    "should_continue_revision", "generate_session_id",
    "run_with_timeout_budget", "log_stage_latency",
    "StageStatus", "StageResult", "TimeoutBudgetResult", "Stage",
    "get_config", "reload_config", "REVISION_HARD_CAP",
]
