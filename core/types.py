"""core/types.py — Shared type definitions for router + tools modules."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


class TaskType(str, Enum):
    CODE     = "code"
    MATH     = "math"
    CREATIVE = "creative"
    ANALYSIS = "analysis"
    GENERAL  = "general"


@dataclass
class RouteResult:
    """Router output: which providers to use and why."""
    task_type:  TaskType
    generator:  str
    critics:    List[str]
    confidence: float = 0.0
    reason:     str   = ""
    overridden: bool  = False


@dataclass
class ProviderStats:
    """Accumulated performance metrics per provider."""
    provider:            str
    attempts:            int   = 0
    successes:           int   = 0
    total_score:         float = 0.0
    total_latency_ms:    float = 0.0
    last_score:          Optional[float] = None
    last_latency_ms:     Optional[float] = None
    updated_at:          Optional[str]   = None
    by_task_type:        Dict[str, Dict[str, float]] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0

    @property
    def avg_score(self) -> float:
        return self.total_score / self.attempts if self.attempts else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.attempts if self.attempts else 0.0

    def record(self, *, success: bool, score: float, latency_ms: float,
               task_type: Optional[TaskType] = None) -> None:
        self.attempts      += 1
        self.successes     += int(success)
        self.total_score   += max(0.0, score)
        self.total_latency_ms += max(0.0, latency_ms)
        self.last_score    = score
        self.last_latency_ms = latency_ms
        self.updated_at    = datetime.now(timezone.utc).isoformat()
        if task_type:
            b = self.by_task_type.setdefault(task_type.value,
                {"attempts": 0.0, "successes": 0.0, "total_score": 0.0})
            b["attempts"]    += 1
            b["successes"]   += int(success)
            b["total_score"] += max(0.0, score)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ToolResult:
    """Output from a tool call (web_search, code_exec, file_read)."""
    tool:       str
    success:    bool
    output:     str   = ""
    error:      Optional[str] = None
    elapsed_ms: float = 0.0

    def to_prompt_block(self) -> str:
        if self.success:
            return f"[Tool: {self.tool}]\n{self.output}\n[/Tool]"
        return f"[Tool: {self.tool} FAILED]\n{self.error}\n[/Tool]"


# ═══════════════════════════════════════════
# R08: Unified Error Hierarchy
# ═══════════════════════════════════════════

class HorcruxError(Exception):
    """Base error for all Horcrux operations."""
    pass


class CallerError(HorcruxError):
    """Base error for AI provider calls."""
    def __init__(self, provider: str, message: str, original: Optional[Exception] = None):
        self.provider = provider
        self.original = original
        super().__init__(f"[{provider}] {message}")


class CallerTimeoutError(CallerError):
    """AI provider call exceeded timeout."""
    pass


class QuotaExhaustedError(CallerError):
    """AI provider quota/rate limit exceeded."""
    pass


class ModelUnavailableError(CallerError):
    """AI model is unavailable (404, maintenance, etc.)."""
    pass


class ParseError(CallerError):
    """Failed to parse AI provider response."""
    pass


class InputError(HorcruxError):
    """Invalid user input."""
    pass


class PersistenceError(HorcruxError):
    """Failed to read/write state or logs."""
    pass


class SecurityError(HorcruxError):
    """Security violation (path traversal, injection, etc.)."""
    pass


# ═══════════════════════════════════════════
# R09: Protocol definitions
# ═══════════════════════════════════════════

@runtime_checkable
class CompactMemoryProtocol(Protocol):
    """Contract for compact memory implementations."""
    def to_dict(self) -> Dict[str, Any]: ...
    def inject_human_directive(self, directive: str) -> None: ...


# Type alias for engine callables (AI provider functions)
EngineCallable = Callable[[str], str]  # prompt → response text


@runtime_checkable
class InteractiveSessionProtocol(Protocol):
    """Contract for interactive session (pause/resume/rollback)."""
    session_id: str
    def check_pause_point(self, point: str) -> None: ...
    def to_dict(self) -> Dict[str, Any]: ...
