"""
core/__init__.py — Debate Chain Core Modules

개선사항 #1~#10 통합 패키지.
"""

from .security     import redact, sanitize_prompt, run_cli_stdin, load_secret
from .job_store    import SQLiteJobStore, JobStatus, JobRecord, get_store
from .provider     import (
    ProviderBackend, ProviderResponse,
    ClaudeCLIBackend, CodexCLIBackend, GeminiAPIBackend,
    ClaudeSDKBackend, OpenAISDKBackend, FallbackProvider,
    make_claude, make_codex, make_gemini,
)
from .async_worker import AsyncWorkerPool, get_pool
from .sse          import SSEBus, SSESubscription, get_bus, make_sse_response
from .cost_tracker import CostTracker, RateLimiter, get_tracker, get_limiter
from .convergence  import ConvergenceAnalyzer, ConvergenceResult, ConvergenceThresholds
from .types        import TaskType, RouteResult, ProviderStats, ToolResult
from .router       import ProviderRouter, detect_task_type
from .tools        import web_search, code_exec, file_read, inject_tools

__all__ = [
    # security
    "redact", "sanitize_prompt", "run_cli_stdin", "load_secret",
    # job_store
    "SQLiteJobStore", "JobStatus", "JobRecord", "get_store",
    # provider
    "ProviderBackend", "ProviderResponse",
    "ClaudeCLIBackend", "CodexCLIBackend", "GeminiAPIBackend",
    "ClaudeSDKBackend", "OpenAISDKBackend", "FallbackProvider",
    "make_claude", "make_codex", "make_gemini",
    # async
    "AsyncWorkerPool", "get_pool",
    # sse
    "SSEBus", "SSESubscription", "get_bus", "make_sse_response",
    # cost
    "CostTracker", "RateLimiter", "get_tracker", "get_limiter",
    # convergence
    "ConvergenceAnalyzer", "ConvergenceResult", "ConvergenceThresholds",
    # types
    "TaskType", "RouteResult", "ProviderStats", "ToolResult",
    # router
    "ProviderRouter", "detect_task_type",
    # tools
    "web_search", "code_exec", "file_read", "inject_tools",
]
