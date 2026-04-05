"""
core/adaptive/interactive.py — Interactive Session Management v3.0

Durable session runtime with pause/resume/rollback/feedback.
Based on Horcrux Interactive Mode 상세 설계 문서 v3.0.

3가지 모드:
  - batch: 기존 동작 (중단 없이 끝까지)
  - interactive: 매 라운드 후 pause
  - semi_interactive: 조건 충족 시 auto-pause

핵심 설계:
  - threading.Event 기반 cooperative pause (LLM 호출 중간에 강제 중단 안 함)
  - VALID_TRANSITIONS 상태 전이 검증
  - DirectiveInjector — 우선순위 기반 prompt 주입 (SYSTEM > HUMAN > CRITIC > SYNTHESIS > AMBIENT)
  - AutoPauseEvaluator — 5가지 auto-pause 조건 독립 평가
  - Atomic checkpoint (tmp → os.replace) with full snapshot
  - SideEffectJournal — rollback 시 reversible 부작용 보상
  - Command queue idempotency
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional


# ═══════════════════════════════════════════
#  Enums
# ═══════════════════════════════════════════

class SessionState(str, Enum):
    IDLE              = "idle"
    RUNNING           = "running"
    PAUSE_REQUESTED   = "pause_requested"
    PAUSED            = "paused"
    AWAITING_FEEDBACK = "awaiting_feedback"
    ROLLING_BACK      = "rolling_back"
    COMPLETED         = "completed"
    FAILED            = "failed"
    CANCELLED         = "cancelled"


VALID_TRANSITIONS: Dict[SessionState, set] = {
    SessionState.IDLE: {SessionState.RUNNING},
    SessionState.RUNNING: {SessionState.PAUSE_REQUESTED, SessionState.COMPLETED, SessionState.FAILED, SessionState.CANCELLED},
    SessionState.PAUSE_REQUESTED: {SessionState.PAUSED, SessionState.COMPLETED, SessionState.FAILED, SessionState.CANCELLED},
    SessionState.PAUSED: {SessionState.RUNNING, SessionState.ROLLING_BACK, SessionState.CANCELLED},
    SessionState.AWAITING_FEEDBACK: {SessionState.PAUSED, SessionState.RUNNING, SessionState.CANCELLED},
    SessionState.ROLLING_BACK: {SessionState.PAUSED, SessionState.FAILED},
    SessionState.COMPLETED: set(),
    SessionState.FAILED: {SessionState.IDLE},
    SessionState.CANCELLED: {SessionState.IDLE},
}


class ExecutionMode(str, Enum):
    BATCH            = "batch"
    INTERACTIVE      = "interactive"
    SEMI_INTERACTIVE = "semi_interactive"


class DirectivePriority(int, Enum):
    SYSTEM    = 1000
    HUMAN     = 100
    CRITIC    = 50
    SYNTHESIS = 25
    AMBIENT   = 10


class InterruptionPoint(str, Enum):
    ROUND_BOUNDARY = "round_boundary"
    PHASE_BOUNDARY = "phase_boundary"
    PRE_TOOL_CALL  = "pre_tool_call"
    POST_TOOL_CALL = "post_tool_call"
    PRE_LLM_CALL   = "pre_llm_call"


class FeedbackAction(str, Enum):
    CONTINUE = "continue"
    FEEDBACK = "feedback"
    FOCUS    = "focus"
    STOP     = "stop"
    ROLLBACK = "rollback"


class SideEffectType(str, Enum):
    REVERSIBLE   = "reversible"
    IDEMPOTENT   = "idempotent"
    IRREVERSIBLE = "irreversible"


# ═══════════════════════════════════════════
#  Dataclasses
# ═══════════════════════════════════════════

@dataclass
class PausePolicy:
    allowed_points: set = field(default_factory=lambda: {
        InterruptionPoint.ROUND_BOUNDARY,
        InterruptionPoint.PHASE_BOUNDARY,
    })
    tool_call_timeout_ms: int = 30000
    llm_call_cancel_policy: str = "wait"
    force_pause_after_ms: int = 60000

    def can_pause_at(self, point: InterruptionPoint) -> bool:
        return point in self.allowed_points


@dataclass
class FocusConstraint:
    target_phases: Optional[List[str]] = None
    max_rounds: Optional[int] = None
    content_filter: Optional[str] = None
    applies_until_round: Optional[int] = None

    def is_active_for(self, phase: str, round_num: int) -> bool:
        if self.target_phases and phase not in self.target_phases:
            return False
        if self.applies_until_round is not None and round_num > self.applies_until_round:
            return False
        return True


@dataclass
class HumanDirective:
    content: str
    priority: DirectivePriority = DirectivePriority.HUMAN
    target_round: Optional[int] = None
    focus_constraint: Optional[FocusConstraint] = None
    source: str = "human"
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)

    def applies_from_round(self, current_round: int) -> int:
        return self.target_round or (current_round + 1)

    def serialize(self) -> dict:
        data = {
            "content": self.content,
            "priority": self.priority.value,
            "target_round": self.target_round,
            "source": self.source,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
        }
        if self.focus_constraint:
            data["focus_constraint"] = asdict(self.focus_constraint)
        return data

    @classmethod
    def deserialize(cls, data: dict) -> "HumanDirective":
        payload = dict(data)
        payload["priority"] = DirectivePriority(payload.get("priority", 100))
        fc = payload.pop("focus_constraint", None)
        if fc:
            payload["focus_constraint"] = FocusConstraint(**fc)
        return cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})


@dataclass
class AutoPauseConfig:
    on_score_drop: bool = True
    score_drop_threshold: float = 0.5
    on_convergence_stall: bool = True
    convergence_stall_rounds: int = 3
    on_critic_disagreement: bool = True
    critic_disagreement_threshold: float = 2.0
    on_irreversible_action: bool = True
    on_cost_threshold: bool = True
    cost_threshold_percent: float = 80.0


@dataclass
class SessionConfig:
    mode: str = "batch"  # batch | interactive | semi_interactive
    max_rounds: int = 10
    pause_policy: PausePolicy = field(default_factory=PausePolicy)
    auto_pause: AutoPauseConfig = field(default_factory=AutoPauseConfig)
    checkpoint_retention: int = 10
    feedback_timeout_seconds: float = 300.0
    side_effect_policy: str = "warn_on_irreversible"


@dataclass
class SessionCommand:
    action: FeedbackAction
    human_directive: str = ""
    focus_area: str = ""
    focus_depth: str = "deep"
    rollback_to_round: int = 0
    new_directive: str = ""
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class RoundResult:
    round_num: int = 0
    thesis: str = ""
    antithesis: str = ""
    synthesis: str = ""
    critic_scores: Dict[str, float] = field(default_factory=dict)
    final_score: float = 0.0
    convergence_delta: float = 0.0
    side_effects: List[dict] = field(default_factory=list)
    human_directives_applied: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    phase_durations: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "round_num": self.round_num,
            "final_score": self.final_score,
            "convergence_delta": self.convergence_delta,
            "critic_scores": self.critic_scores,
            "human_directives_applied": self.human_directives_applied,
            "duration_seconds": self.duration_seconds,
            "side_effects_count": len(self.side_effects),
        }


# ═══════════════════════════════════════════
#  SideEffectJournal
# ═══════════════════════════════════════════

@dataclass
class SideEffectEntry:
    round_num: int
    phase: str
    effect_type: SideEffectType
    tool_name: str
    tool_args: dict = field(default_factory=dict)
    result_summary: str = ""
    timestamp: float = field(default_factory=time.time)
    # R05: 감사 전용 — 실행되지 않음. 향후 typed action으로 전환 필요.
    compensating_action: Optional[str] = None  # audit-only, never eval'd
    compensated: bool = False

    def serialize(self) -> dict:
        d = asdict(self)
        d["effect_type"] = self.effect_type.value
        return d

    @classmethod
    def deserialize(cls, data: dict) -> "SideEffectEntry":
        p = dict(data)
        p["effect_type"] = SideEffectType(p["effect_type"])
        return cls(**{k: v for k, v in p.items() if k in cls.__dataclass_fields__})


class SideEffectJournal:
    def __init__(self):
        self._entries: List[SideEffectEntry] = []

    def record(self, entry: SideEffectEntry):
        self._entries.append(entry)

    def get_for_round(self, round_num: int) -> List[SideEffectEntry]:
        return [e for e in self._entries if e.round_num == round_num]

    def has_irreversible_after(self, round_num: int) -> bool:
        return any(
            e.effect_type == SideEffectType.IRREVERSIBLE and e.round_num > round_num
            for e in self._entries
        )

    def irreversible_rounds_after(self, round_num: int) -> List[int]:
        return sorted(set(
            e.round_num for e in self._entries
            if e.effect_type == SideEffectType.IRREVERSIBLE and e.round_num > round_num
        ))

    def compensate_after(self, round_num: int) -> List[str]:
        compensated = []
        for e in reversed(self._entries):
            if e.round_num > round_num and e.effect_type == SideEffectType.REVERSIBLE:
                if e.compensating_action and not e.compensated:
                    compensated.append(e.compensating_action)
                    e.compensated = True
        return compensated

    def truncate_to_round(self, round_num: int):
        self._entries = [e for e in self._entries if e.round_num <= round_num]

    def serialize(self) -> List[dict]:
        return [e.serialize() for e in self._entries]

    @classmethod
    def deserialize(cls, data: List[dict]) -> "SideEffectJournal":
        j = cls()
        j._entries = [SideEffectEntry.deserialize(x) for x in data]
        return j

    @property
    def summary(self) -> dict:
        rev = sum(1 for e in self._entries if e.effect_type == SideEffectType.REVERSIBLE)
        irr = sum(1 for e in self._entries if e.effect_type == SideEffectType.IRREVERSIBLE)
        return {"reversible": rev, "irreversible": irr, "total": len(self._entries)}


# ═══════════════════════════════════════════
#  AutoPauseEvaluator
# ═══════════════════════════════════════════

class AutoPauseEvaluator:
    def __init__(self, config: AutoPauseConfig):
        self._config = config
        self._history: List[dict] = []

    def evaluate(
        self,
        current: RoundResult,
        all_results: List[RoundResult],
        side_effects: SideEffectJournal,
    ) -> Optional[str]:
        reason = None

        # 1. score divergence
        if self._config.on_score_drop and len(all_results) >= 2:
            prev = all_results[-2].final_score
            drop = prev - current.final_score
            if drop >= self._config.score_drop_threshold:
                reason = f"score_divergence: {prev:.2f} → {current.final_score:.2f} (delta={drop:.2f})"

        # 2. consensus stall
        if self._config.on_convergence_stall and not reason:
            n = self._config.convergence_stall_rounds
            if len(all_results) >= n:
                recent = all_results[-n:]
                if all(abs(r.convergence_delta) < 0.01 for r in recent):
                    reason = f"consensus_stall: {n} rounds with delta < 0.01"

        # 3. confidence drop (critic disagreement)
        if self._config.on_critic_disagreement and not reason:
            scores = list(current.critic_scores.values())
            if len(scores) >= 2:
                spread = max(scores) - min(scores)
                if spread >= self._config.critic_disagreement_threshold:
                    reason = f"confidence_drop: critic spread={spread:.2f}"

        # 4. critic severity (irreversible)
        if self._config.on_irreversible_action and not reason:
            irr = [e for e in side_effects.get_for_round(current.round_num)
                   if e.effect_type == SideEffectType.IRREVERSIBLE]
            if irr:
                reason = f"critic_severity: irreversible actions={[e.tool_name for e in irr]}"

        # 5. cost exceeded
        if self._config.on_cost_threshold and not reason:
            total_dur = sum(r.duration_seconds for r in all_results)
            max_dur = len(all_results) * 60
            if max_dur > 0:
                pct = (total_dur / max_dur) * 100
                if pct >= self._config.cost_threshold_percent:
                    reason = f"cost_exceeded: usage={pct:.1f}%"

        self._history.append({
            "round": current.round_num,
            "triggered": reason is not None,
            "reason": reason,
            "timestamp": time.time(),
        })
        return reason

    def record_timeout_resume(self, round_num: int):
        self._history.append({"round": round_num, "triggered": False, "reason": "timeout_resume", "timestamp": time.time()})

    def export_history(self) -> List[dict]:
        return list(self._history)

    def restore_history(self, history: List[dict]):
        self._history = list(history)


# ═══════════════════════════════════════════
#  DirectiveInjector
# ═══════════════════════════════════════════

class DirectiveInjector:
    """우선순위 기반 prompt 주입. SYSTEM > HUMAN > CRITIC > SYNTHESIS > AMBIENT."""

    def __init__(self, memory=None):
        self._memory = memory
        self._directives: List[HumanDirective] = []
        self._applied_log: Dict[int, List[str]] = {}

    def inject(self, directive: HumanDirective, current_round: int):
        if directive.focus_constraint and directive.focus_constraint.max_rounds:
            applies_from = directive.applies_from_round(current_round)
            directive.focus_constraint.applies_until_round = applies_from + directive.focus_constraint.max_rounds - 1
        self._directives.append(directive)
        if self._memory and hasattr(self._memory, "inject_human_directive"):
            self._memory.inject_human_directive(directive.content, directive.source)

    def build_phase_prompt(self, phase: str, round_num: int, base_context: str) -> str:
        applicable = self._resolve_applicable(phase, round_num)
        merged = self._render(base_context, applicable)
        self._applied_log.setdefault(round_num, []).extend([d.idempotency_key for d in applicable])
        return merged

    def get_human_directive_text(self, round_num: int) -> Optional[str]:
        applicable = [d for d in self._directives
                      if d.priority == DirectivePriority.HUMAN
                      and d.applies_from_round(round_num - 1) <= round_num]
        if not applicable:
            return None
        return "\n".join(d.content for d in applicable)

    def get_focus_text(self, round_num: int) -> Optional[str]:
        for d in reversed(self._directives):
            if d.focus_constraint and d.applies_from_round(round_num - 1) <= round_num:
                if d.focus_constraint.is_active_for("", round_num):
                    return d.focus_constraint.content_filter or d.content
        return None

    def _resolve_applicable(self, phase: str, round_num: int) -> List[HumanDirective]:
        active = []
        for d in self._directives:
            if d.applies_from_round(round_num - 1) > round_num:
                continue
            if d.focus_constraint and not d.focus_constraint.is_active_for(phase, round_num):
                continue
            active.append(d)
        return self._apply_priority_rules(active)

    def _apply_priority_rules(self, directives: List[HumanDirective]) -> List[HumanDirective]:
        sorted_d = sorted(directives, key=lambda d: d.priority.value, reverse=True)
        human = [d for d in sorted_d if d.priority == DirectivePriority.HUMAN]
        others = [d for d in sorted_d if d.priority != DirectivePriority.HUMAN]
        has_override = any(
            any(t in d.content.lower() for t in ["ignore critic", "override", "무시", "대신"])
            for d in human
        )
        if has_override:
            others = [d for d in others if d.priority != DirectivePriority.CRITIC]
        return human + others

    def _render(self, base: str, directives: List[HumanDirective]) -> str:
        sections = [base]
        if directives:
            sections.append("\n[Human / Priority Guidance]")
            for d in directives:
                sections.append(f"- ({d.priority.name}) {d.content}")
        return "\n".join(sections)

    def get_applied_keys(self, round_num: int) -> List[str]:
        return self._applied_log.get(round_num, [])

    def export_active_directives(self) -> List[dict]:
        return [d.serialize() for d in self._directives]

    def restore_directives(self, data: List[dict]):
        self._directives = [HumanDirective.deserialize(x) for x in data]


# ═══════════════════════════════════════════
#  CheckpointStore
# ═══════════════════════════════════════════

@dataclass
class Checkpoint:
    checkpoint_id: str
    session_id: str
    round_num: int
    state: str
    memory_snapshot: dict
    round_results: List[dict]
    active_directives: List[dict]
    auto_pause_history: List[dict]
    side_effect_journal: List[dict]
    engine_state: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def serialize(self) -> dict:
        return asdict(self)

    @classmethod
    def deserialize(cls, data: dict) -> "Checkpoint":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class CheckpointStore:
    def __init__(self, session_id: str, base_dir: str = "checkpoints", retention: int = 10):
        # R04: session_id path traversal 차단
        safe_id = re.sub(r'[^a-zA-Z0-9_\-]', '_', session_id)
        self._dir = Path(base_dir) / safe_id
        if not self._dir.resolve().is_relative_to(Path(base_dir).resolve()):
            raise ValueError(f"Invalid session_id: path traversal detected: {session_id}")
        self._dir.mkdir(parents=True, exist_ok=True)
        self._retention = retention

    def save(self, checkpoint: Checkpoint):
        target = self._dir / f"round_{checkpoint.round_num}.json"
        tmp = self._dir / f"round_{checkpoint.round_num}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(checkpoint.serialize(), f, ensure_ascii=False, indent=2)
        os.replace(str(tmp), str(target))
        self._prune()

    def load(self, round_num: int) -> Optional[Checkpoint]:
        path = self._dir / f"round_{round_num}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Checkpoint.deserialize(data)
        except (json.JSONDecodeError, KeyError):
            return None

    def available_rounds(self) -> List[int]:
        rounds = []
        for f in self._dir.glob("round_*.json"):
            try:
                rounds.append(int(f.stem.split("_")[1]))
            except (ValueError, IndexError):
                pass
        return sorted(rounds)

    def _prune(self):
        rounds = self.available_rounds()
        while len(rounds) > self._retention:
            oldest = rounds.pop(0)
            p = self._dir / f"round_{oldest}.json"
            if p.exists():
                p.unlink()


# ═══════════════════════════════════════════
#  Exceptions
# ═══════════════════════════════════════════

class HorcruxInteractiveError(Exception):
    pass

class InvalidStateTransition(HorcruxInteractiveError):
    def __init__(self, current: SessionState, attempted: SessionState):
        super().__init__(f"Cannot transition from {current.value} to {attempted.value}")

class FeedbackTimeout(HorcruxInteractiveError):
    pass

class RollbackFailed(HorcruxInteractiveError):
    pass

class IrreversibleSideEffectError(HorcruxInteractiveError):
    def __init__(self, effects: List[SideEffectEntry]):
        self.effects = effects
        super().__init__(f"Irreversible side effects in rounds: {[e.round_num for e in effects]}")


# ═══════════════════════════════════════════
#  InteractiveSession
# ═══════════════════════════════════════════

class InteractiveSession:
    """
    Durable session runtime with command queue serialization.
    debate loop thread와 command processing이 cooperative check point에서 동기화.
    """

    def __init__(self, config: SessionConfig, base_dir: str = "checkpoints"):
        self.session_id = f"int_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.config = config
        self.state = SessionState.IDLE
        self.current_round = 0
        self.rounds: List[RoundResult] = []

        self.checkpoint_store = CheckpointStore(self.session_id, base_dir, config.checkpoint_retention)
        self.side_effects = SideEffectJournal()
        self.auto_pause_eval = AutoPauseEvaluator(config.auto_pause)
        self.directive_injector: Optional[DirectiveInjector] = None

        self._pause_event = threading.Event()
        self._pause_event.set()
        self._feedback_event = threading.Event()
        self._pause_reason: Optional[str] = None
        self._compact_memory = None
        self._task: str = ""
        self._thread: Optional[threading.Thread] = None
        self._processed_keys: set = set()

    def _transition_to(self, new_state: SessionState):
        valid = VALID_TRANSITIONS.get(self.state, set())
        if new_state not in valid:
            raise InvalidStateTransition(self.state, new_state)
        self.state = new_state

    # ─── External API ───

    def start(self, task: str, engine_fn: Callable, compact_memory=None):
        self._task = task
        self._compact_memory = compact_memory
        self.directive_injector = DirectiveInjector(compact_memory)
        self._transition_to(SessionState.RUNNING)
        self._thread = threading.Thread(target=self._run_wrapper, args=(engine_fn,), daemon=True)
        self._thread.start()

    def pause(self, reason: str = "user_requested"):
        if self.state == SessionState.RUNNING:
            self._transition_to(SessionState.PAUSE_REQUESTED)
            self._pause_reason = reason
            self._pause_event.clear()

    def resume(self, command: SessionCommand):
        # idempotency check
        if command.idempotency_key in self._processed_keys:
            return
        self._processed_keys.add(command.idempotency_key)

        if self.state not in (SessionState.PAUSED, SessionState.AWAITING_FEEDBACK):
            if command.action == FeedbackAction.STOP:
                self.state = SessionState.CANCELLED
                self._pause_event.set()
                self._feedback_event.set()
                return
            return

        if command.action == FeedbackAction.STOP:
            self.state = SessionState.CANCELLED
            self._pause_event.set()
            self._feedback_event.set()
            return

        if command.action == FeedbackAction.ROLLBACK:
            self._do_rollback(command.rollback_to_round, command.new_directive)
            return

        if command.action == FeedbackAction.FEEDBACK and command.human_directive:
            directive = HumanDirective(
                content=command.human_directive,
                priority=DirectivePriority.HUMAN,
                source="feedback",
            )
            self.directive_injector.inject(directive, self.current_round)

        if command.action == FeedbackAction.FOCUS and command.focus_area:
            directive = HumanDirective(
                content=command.focus_area,
                priority=DirectivePriority.HUMAN,
                source="focus",
                focus_constraint=FocusConstraint(
                    content_filter=command.focus_area,
                    max_rounds=3 if command.focus_depth == "deep" else 1,
                ),
            )
            self.directive_injector.inject(directive, self.current_round)

        self._transition_to(SessionState.RUNNING)
        self._pause_event.set()
        self._feedback_event.set()

    def check_pause_point(self, point: str = "") -> bool:
        """Cooperative pause check — debate loop에서 호출."""
        if self.config.mode == "batch":
            return True

        if self.state == SessionState.PAUSE_REQUESTED:
            self._transition_to(SessionState.PAUSED)

        if self.state == SessionState.CANCELLED:
            return False

        if self.config.mode == "interactive" and self.state == SessionState.RUNNING:
            self._transition_to(SessionState.PAUSE_REQUESTED)
            self._pause_reason = f"interactive_round_{self.current_round}"
            self._pause_event.clear()
            self._transition_to(SessionState.PAUSED)

        if self.state == SessionState.PAUSED:
            self.state = SessionState.AWAITING_FEEDBACK
            self._pause_event.wait(timeout=self.config.feedback_timeout_seconds)
            if not self._pause_event.is_set():
                # timeout → auto continue
                self._transition_to(SessionState.RUNNING)
                self._pause_event.set()

        return self.state not in (SessionState.CANCELLED, SessionState.FAILED)

    def should_auto_pause(self, round_result: RoundResult) -> Optional[str]:
        if self.config.mode != "semi_interactive":
            return None
        return self.auto_pause_eval.evaluate(round_result, self.rounds, self.side_effects)

    def create_checkpoint(self):
        cp = Checkpoint(
            checkpoint_id=f"ckpt_{uuid.uuid4().hex[:12]}",
            session_id=self.session_id,
            round_num=self.current_round,
            state=self.state.value,
            memory_snapshot=self._compact_memory.to_dict() if self._compact_memory else {},
            round_results=[r.to_dict() for r in self.rounds],
            active_directives=self.directive_injector.export_active_directives() if self.directive_injector else [],
            auto_pause_history=self.auto_pause_eval.export_history(),
            side_effect_journal=self.side_effects.serialize(),
        )
        self.checkpoint_store.save(cp)

    def get_pending_directive(self) -> Optional[str]:
        if not self.directive_injector:
            return None
        return self.directive_injector.get_human_directive_text(self.current_round + 1)

    def get_pending_focus(self) -> Optional[str]:
        if not self.directive_injector:
            return None
        return self.directive_injector.get_focus_text(self.current_round + 1)

    # ─── Internal ───

    def _run_wrapper(self, engine_fn: Callable):
        try:
            engine_fn(self)
            if self.state not in (SessionState.CANCELLED, SessionState.FAILED):
                self.state = SessionState.COMPLETED
        except Exception:
            self.state = SessionState.FAILED

    def _do_rollback(self, to_round: int, new_directive: str = ""):
        self._transition_to(SessionState.ROLLING_BACK)

        # compensate reversible
        compensated = self.side_effects.compensate_after(to_round)
        self.side_effects.truncate_to_round(to_round)

        # restore checkpoint
        cp = self.checkpoint_store.load(to_round)
        if cp:
            self.current_round = to_round
            self.rounds = self.rounds[:to_round]
            if cp.active_directives and self.directive_injector:
                self.directive_injector.restore_directives(cp.active_directives)
            if cp.auto_pause_history:
                self.auto_pause_eval.restore_history(cp.auto_pause_history)

        if new_directive:
            directive = HumanDirective(content=new_directive, source="rollback")
            if self.directive_injector:
                self.directive_injector.inject(directive, to_round)

        self._transition_to(SessionState.PAUSED)
        self._pause_reason = f"rolled_back_to_round_{to_round}"
        irr = self.side_effects.irreversible_rounds_after(to_round)
        if irr:
            self._pause_reason += f" (irreversible_in_rounds: {irr})"

    # ─── Serialization ───

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "status": self.state.value,
            "current_round": self.current_round,
            "pause_reason": self._pause_reason,
            "mode": self.config.mode,
            "max_rounds": self.config.max_rounds,
            "rounds": [r.to_dict() for r in self.rounds],
            "available_actions": self._available_actions(),
            "checkpoints": self.checkpoint_store.available_rounds(),
            "side_effects_summary": self.side_effects.summary,
            "auto_pause_history": self.auto_pause_eval.export_history(),
        }

    def _available_actions(self) -> List[str]:
        if self.state in (SessionState.PAUSED, SessionState.AWAITING_FEEDBACK):
            actions = ["continue", "feedback", "focus", "stop"]
            if self.checkpoint_store.available_rounds():
                actions.append("rollback")
            return actions
        if self.state == SessionState.RUNNING:
            return ["pause", "cancel"]
        return []
