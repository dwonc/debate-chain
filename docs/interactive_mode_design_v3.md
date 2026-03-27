# Horcrux Interactive Mode 상세 설계 문서 v3.0

## 문서 핵심
- **Interactive Mode의 핵심은 UI가 아니라 `InteractiveSession`이라는 durable session runtime이며, rollback과 재시작 복구를 설계 초기에 포함해야 운영에서 깨지지 않는다.**
- **`human_directive`는 critic 메모의 부속물이 아니라 `CompactMemory`와 동급의 1급 상태이며, 가중치 기반 우선순위 시스템(`SYSTEM > HUMAN > CRITIC > SYNTHESIS > AMBIENT`)으로 prompt에 주입된다.**
- **Semi-interactive의 5가지 auto-pause 조건(score divergence, consensus stall, confidence drop, cost exceeded, critic severity)은 알고리즘적으로 평가되어 사용자 개입이 필요한 시점을 자동 감지한다.**

---

## 1. InteractiveSession 클래스 설계

### 1.1 설계 목표
- 기존 batch debate loop를 라운드 단위 pause/resume 가능한 durable session runtime으로 전환한다.
- 사용자 개입은 단순 UI 이벤트가 아니라 세션 상태 전이와 checkpoint 복원 가능한 명령으로 처리한다.
- rollback, crash recovery, feedback ordering, idempotency를 초기에 내장한다.

### 1.2 State Machine

text
IDLE ──start()──► RUNNING ──pause()──► PAUSE_REQUESTED
                    │                       │
                    │               [cooperative check]
                    │                       │
                    │                       ▼
                    │                   PAUSED ──resume()──► RUNNING
                    │                     │
                    │              submit_feedback()
                    │                     │
                    │                     ▼
                    │              AWAITING_FEEDBACK ──(auto)──► PAUSED
                    │                                             │
                    ▼                                        rollback()
                 COMPLETED                                       │
                    │                                            ▼
                 FAILED                                    ROLLING_BACK ──► PAUSED
                    │
                 CANCELLED


### 1.3 상태 전이 규칙
- `PAUSE_REQUESTED`는 transient state다. debate loop 내부 cooperative check point에서만 `PAUSED`로 전이된다.
- `AWAITING_FEEDBACK`는 `semi_interactive` 모드에서 auto-pause 발생 시 사용한다.
- `ROLLING_BACK`은 rollback 실행 중 상태이며, 완료 후 `PAUSED`로 전이된다.
- `CANCELLED`, `COMPLETED`, `FAILED`는 terminal state다. 단, 운영 편의를 위해 `FAILED -> IDLE`, `CANCELLED -> IDLE` 복귀를 허용할 수 있다.

### 1.4 Core Enums / Dataclasses

python
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


class SessionState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    AWAITING_FEEDBACK = "awaiting_feedback"
    ROLLING_BACK = "rolling_back"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


VALID_TRANSITIONS: dict[SessionState, set[SessionState]] = {
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


class CommandType(Enum):
    PAUSE = "pause"
    RESUME = "resume"
    SUBMIT_FEEDBACK = "submit_feedback"
    ROLLBACK = "rollback"
    CANCEL = "cancel"


class ExecutionMode(Enum):
    BATCH = "batch"
    INTERACTIVE = "interactive"
    SEMI_INTERACTIVE = "semi_interactive"


class DirectivePriority(Enum):
    SYSTEM = 1000
    HUMAN = 100
    CRITIC = 50
    SYNTHESIS = 25
    AMBIENT = 10


@dataclass
class SessionCommand:
    cmd_type: CommandType
    payload: dict = field(default_factory=dict)
    future: Future = field(default_factory=Future)
    sequence_num: int = 0
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.monotonic)


### 1.5 Focus Constraint / Human Directive

python
@dataclass
class FocusConstraint:
    target_phases: Optional[list[str]] = None
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
    metadata: dict[str, Any] = field(default_factory=dict)

    def applies_from_round(self, current_round: int) -> int:
        return self.target_round or (current_round + 1)

    def serialize(self) -> dict:
        data = asdict(self)
        data["priority"] = self.priority.value
        return data

    @classmethod
    def deserialize(cls, data: dict) -> "HumanDirective":
        payload = dict(data)
        payload["priority"] = DirectivePriority(payload["priority"])
        if payload.get("focus_constraint"):
            payload["focus_constraint"] = FocusConstraint(**payload["focus_constraint"])
        return cls(**payload)


### 1.6 Side-Effect Tracking

python
class SideEffectType(Enum):
    REVERSIBLE = "reversible"
    IDEMPOTENT = "idempotent"
    IRREVERSIBLE = "irreversible"


@dataclass
class SideEffectEntry:
    round_num: int
    phase: str
    effect_type: SideEffectType
    tool_name: str
    tool_args: dict
    result_summary: str
    timestamp: float = field(default_factory=time.time)
    compensating_action: Optional[str] = None
    compensated: bool = False

    def serialize(self) -> dict:
        data = asdict(self)
        data["effect_type"] = self.effect_type.value
        return data

    @classmethod
    def deserialize(cls, data: dict) -> "SideEffectEntry":
        payload = dict(data)
        payload["effect_type"] = SideEffectType(payload["effect_type"])
        return cls(**payload)


class SideEffectJournal:
    def __init__(self):
        self._entries: list[SideEffectEntry] = []

    def record(self, entry: SideEffectEntry) -> None:
        self._entries.append(entry)

    def get_entries_after_round(self, round_num: int) -> list[SideEffectEntry]:
        return [e for e in self._entries if e.round_num > round_num]

    def get_entries_for_round(self, round_num: int) -> list[SideEffectEntry]:
        return [e for e in self._entries if e.round_num == round_num]

    def get_irreversible_after_round(self, round_num: int) -> list[SideEffectEntry]:
        return [
            e for e in self._entries
            if e.round_num > round_num and e.effect_type == SideEffectType.IRREVERSIBLE
        ]

    def compensate_reversible_after_round(self, round_num: int) -> list[str]:
        compensated: list[str] = []
        for entry in reversed(self._entries):
            if entry.round_num > round_num and entry.effect_type == SideEffectType.REVERSIBLE:
                if entry.compensating_action and not entry.compensated:
                    compensated.append(entry.compensating_action)
                    entry.compensated = True
        return compensated

    def truncate_to_round(self, round_num: int) -> None:
        self._entries = [e for e in self._entries if e.round_num <= round_num]

    def serialize(self) -> list[dict]:
        return [e.serialize() for e in self._entries]

    @classmethod
    def deserialize(cls, data: list[dict]) -> "SideEffectJournal":
        journal = cls()
        journal._entries = [SideEffectEntry.deserialize(x) for x in data]
        return journal


### 1.7 Cooperative Interruption Points / Pause Policy

python
class InterruptionPoint(Enum):
    ROUND_BOUNDARY = "round_boundary"
    PHASE_BOUNDARY = "phase_boundary"
    PRE_TOOL_CALL = "pre_tool_call"
    POST_TOOL_CALL = "post_tool_call"
    PRE_LLM_CALL = "pre_llm_call"


@dataclass
class PausePolicy:
    allowed_points: set[InterruptionPoint] = field(default_factory=lambda: {
        InterruptionPoint.ROUND_BOUNDARY,
        InterruptionPoint.PHASE_BOUNDARY,
    })
    tool_call_timeout_ms: int = 30000
    llm_call_cancel_policy: str = "wait"  # wait | cancel_and_retry
    force_pause_after_ms: int = 60000

    def can_pause_at(self, point: InterruptionPoint) -> bool:
        return point in self.allowed_points


### 1.8 Checkpoint Schema / Store

python
@dataclass
class Checkpoint:
    checkpoint_id: str
    session_id: str
    round_num: int
    state: SessionState
    memory_snapshot: dict
    round_results: list[dict]
    active_directives: list[dict]
    auto_pause_history: list[dict]
    side_effect_journal: list[dict]
    engine_state: dict
    created_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def serialize(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def deserialize(cls, data: dict) -> "Checkpoint":
        payload = dict(data)
        payload["state"] = SessionState(payload["state"])
        return cls(**payload)


class CheckpointStore:
    def __init__(self, base_dir: Path, retention_policy: int = 10):
        self._base_dir = base_dir
        self._retention_policy = retention_policy
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, checkpoint: Checkpoint) -> Path:
        target = self._base_dir / checkpoint.session_id / f"round_{checkpoint.round_num}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(checkpoint.serialize(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(target))
        self._enforce_retention(checkpoint.session_id)
        return target

    def load(self, session_id: str, round_num: int) -> Optional[Checkpoint]:
        path = self._base_dir / session_id / f"round_{round_num}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Checkpoint.deserialize(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            return None

    def list_checkpoints(self, session_id: str) -> list[int]:
        session_dir = self._base_dir / session_id
        if not session_dir.exists():
            return []
        rounds: list[int] = []
        for file in session_dir.glob("round_*.json"):
            try:
                rounds.append(int(file.stem.split("_")[1]))
            except (IndexError, ValueError):
                continue
        return sorted(rounds)

    def _enforce_retention(self, session_id: str) -> None:
        rounds = self.list_checkpoints(session_id)
        while len(rounds) > self._retention_policy:
            oldest = rounds.pop(0)
            (self._base_dir / session_id / f"round_{oldest}.json").unlink(missing_ok=True)


### 1.9 Session Config / Auto-Pause Config

python
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

    def serialize(self) -> dict:
        return asdict(self)

    @classmethod
    def deserialize(cls, data: dict) -> "AutoPauseConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SessionConfig:
    mode: ExecutionMode = ExecutionMode.INTERACTIVE
    max_rounds: int = 10
    pause_policy: PausePolicy = field(default_factory=PausePolicy)
    auto_pause: AutoPauseConfig = field(default_factory=AutoPauseConfig)
    checkpoint_retention: int = 10
    checkpoint_dir: str = "./checkpoints"
    feedback_timeout_seconds: float = 300.0
    command_queue_max_size: int = 100
    side_effect_policy: str = "warn_on_irreversible"  # block | warn_on_irreversible | allow_all

    def serialize(self) -> dict:
        return {
            "mode": self.mode.value,
            "max_rounds": self.max_rounds,
            "pause_policy": {
                "allowed_points": [p.value for p in self.pause_policy.allowed_points],
                "tool_call_timeout_ms": self.pause_policy.tool_call_timeout_ms,
                "llm_call_cancel_policy": self.pause_policy.llm_call_cancel_policy,
                "force_pause_after_ms": self.pause_policy.force_pause_after_ms,
            },
            "auto_pause": self.auto_pause.serialize(),
            "checkpoint_retention": self.checkpoint_retention,
            "checkpoint_dir": self.checkpoint_dir,
            "feedback_timeout_seconds": self.feedback_timeout_seconds,
            "command_queue_max_size": self.command_queue_max_size,
            "side_effect_policy": self.side_effect_policy,
        }

    @classmethod
    def deserialize(cls, data: dict) -> "SessionConfig":
        payload = dict(data)
        if "mode" in payload:
            payload["mode"] = ExecutionMode(payload["mode"])
        if "pause_policy" in payload:
            pp = dict(payload["pause_policy"])
            pp["allowed_points"] = {InterruptionPoint(v) for v in pp.get("allowed_points", [])}
            payload["pause_policy"] = PausePolicy(**pp)
        if "auto_pause" in payload:
            payload["auto_pause"] = AutoPauseConfig.deserialize(payload["auto_pause"])
        return cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})


### 1.10 Round Result / Exceptions

python
@dataclass
class RoundResult:
    round_num: int
    thesis: str
    antithesis: str
    synthesis: str
    critic_scores: dict[str, float]
    final_score: float
    convergence_delta: float
    side_effects: list[dict]
    human_directives_applied: list[str]
    duration_seconds: float
    phase_durations: dict[str, float]

    def serialize(self) -> dict:
        return asdict(self)

    @classmethod
    def deserialize(cls, data: dict) -> "RoundResult":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class HorcruxInteractiveError(Exception):
    pass


class InvalidStateTransition(HorcruxInteractiveError):
    def __init__(self, current: SessionState, attempted: SessionState):
        self.current = current
        self.attempted = attempted
        super().__init__(f"Cannot transition from {current.value} to {attempted.value}")


class SessionNotFound(HorcruxInteractiveError):
    def __init__(self, session_id: str):
        super().__init__(f"Session not found: {session_id}")


class FeedbackTimeout(HorcruxInteractiveError):
    def __init__(self, session_id: str, timeout_seconds: float):
        super().__init__(f"Feedback timeout after {timeout_seconds}s for session {session_id}")


class RollbackFailed(HorcruxInteractiveError):
    def __init__(self, session_id: str, target_round: int, reason: str):
        self.target_round = target_round
        super().__init__(f"Rollback to round {target_round} failed: {reason}")


class IrreversibleSideEffectError(HorcruxInteractiveError):
    def __init__(self, effects: list[SideEffectEntry]):
        self.effects = effects
        super().__init__("Irreversible side effects exist")


class CommandQueueFull(HorcruxInteractiveError):
    pass


### 1.11 InteractiveSession 메인 클래스

python
class InteractiveSession:
    """
    모든 외부 명령을 command queue로 직렬화한다.
    debate loop thread가 queue를 소비하므로 pause/resume/feedback/rollback 순서를 명확히 보장한다.
    """

    def __init__(
        self,
        session_id: str,
        config: SessionConfig,
        engine: "HorcruxDebateEngine",
        memory: "CompactMemory",
    ):
        self.session_id = session_id
        self.config = config
        self._engine = engine
        self._memory = memory
        self._state = SessionState.IDLE
        self._current_round = 0
        self._round_results: list[RoundResult] = []
        self._side_effect_journal = SideEffectJournal()
        self._checkpoint_store = CheckpointStore(Path(config.checkpoint_dir), config.checkpoint_retention)
        self._directive_injector = DirectiveInjector(memory)
        self._auto_pause_evaluator = AutoPauseEvaluator(config.auto_pause)

        self._command_queue: deque[SessionCommand] = deque(maxlen=config.command_queue_max_size)
        self._sequence_counter = 0
        self._processed_idempotency_keys: set[str] = set()

        self._pause_event = threading.Event()
        self._pause_event.set()
        self._pause_acknowledged = threading.Event()
        self._feedback_event = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None

        self._last_auto_pause_reason: Optional[str] = None
        self._created_at = time.time()
        self._updated_at = self._created_at

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def current_round(self) -> int:
        return self._current_round

    def _transition_to(self, new_state: SessionState) -> None:
        if new_state not in VALID_TRANSITIONS.get(self._state, set()):
            raise InvalidStateTransition(self._state, new_state)
        self._state = new_state
        self._updated_at = time.time()

    def start(self) -> str:
        self._transition_to(SessionState.RUNNING)
        self._loop_thread = threading.Thread(
            target=self._debate_loop,
            name=f"horcrux-{self.session_id}",
            daemon=True,
        )
        self._loop_thread.start()
        return self.session_id

    def pause(self, idempotency_key: str | None = None) -> Future:
        return self._submit_command(CommandType.PAUSE, idempotency_key=idempotency_key)

    def resume(self, idempotency_key: str | None = None) -> Future:
        return self._submit_command(CommandType.RESUME, idempotency_key=idempotency_key)

    def submit_feedback(self, directive: HumanDirective, idempotency_key: str | None = None) -> Future:
        return self._submit_command(
            CommandType.SUBMIT_FEEDBACK,
            payload={"directive": directive},
            idempotency_key=idempotency_key or directive.idempotency_key,
        )

    def rollback(self, target_round: int, idempotency_key: str | None = None) -> Future:
        return self._submit_command(
            CommandType.ROLLBACK,
            payload={"target_round": target_round},
            idempotency_key=idempotency_key,
        )

    def cancel(self, idempotency_key: str | None = None) -> Future:
        return self._submit_command(CommandType.CANCEL, idempotency_key=idempotency_key)

    def _submit_command(self, cmd_type: CommandType, payload: dict | None = None, idempotency_key: str | None = None) -> Future:
        if len(self._command_queue) >= self.config.command_queue_max_size:
            raise CommandQueueFull(f"Queue full for session {self.session_id}")

        self._sequence_counter += 1
        cmd = SessionCommand(
            cmd_type=cmd_type,
            payload=payload or {},
            sequence_num=self._sequence_counter,
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )
        self._command_queue.append(cmd)
        return cmd.future

    def _process_pending_commands(self) -> None:
        while self._command_queue:
            cmd = self._command_queue.popleft()
            if cmd.idempotency_key in self._processed_idempotency_keys:
                cmd.future.set_result({"status": "duplicate", "idempotency_key": cmd.idempotency_key})
                continue
            try:
                result = self._execute_command(cmd)
                self._processed_idempotency_keys.add(cmd.idempotency_key)
                cmd.future.set_result(result)
            except Exception as exc:
                cmd.future.set_exception(exc)

    def _execute_command(self, cmd: SessionCommand) -> dict:
        if cmd.cmd_type == CommandType.PAUSE:
            return self._handle_pause()
        if cmd.cmd_type == CommandType.RESUME:
            return self._handle_resume()
        if cmd.cmd_type == CommandType.SUBMIT_FEEDBACK:
            return self._handle_feedback(cmd.payload)
        if cmd.cmd_type == CommandType.ROLLBACK:
            return self._handle_rollback(cmd.payload["target_round"])
        if cmd.cmd_type == CommandType.CANCEL:
            return self._handle_cancel()
        raise ValueError(f"Unknown command: {cmd.cmd_type}")

    def _handle_pause(self) -> dict:
        if self._state == SessionState.RUNNING:
            self._transition_to(SessionState.PAUSE_REQUESTED)
            self._pause_event.clear()
            return {"status": "pause_requested", "round": self._current_round}
        if self._state in (SessionState.PAUSED, SessionState.AWAITING_FEEDBACK):
            return {"status": "already_paused", "round": self._current_round}
        raise InvalidStateTransition(self._state, SessionState.PAUSE_REQUESTED)

    def _handle_resume(self) -> dict:
        if self._state not in (SessionState.PAUSED, SessionState.AWAITING_FEEDBACK):
            raise InvalidStateTransition(self._state, SessionState.RUNNING)
        self._transition_to(SessionState.RUNNING)
        self._pause_event.set()
        self._feedback_event.set()
        return {"status": "resumed", "round": self._current_round}

    def _handle_feedback(self, payload: dict) -> dict:
        directive: HumanDirective = payload["directive"]
        self._directive_injector.inject(directive, current_round=self._current_round)
        self._feedback_event.set()
        return {
            "status": "accepted",
            "directive_id": directive.idempotency_key,
            "applies_from_round": directive.applies_from_round(self._current_round),
        }

    def _handle_rollback(self, target_round: int) -> dict:
        if self._state != SessionState.PAUSED:
            raise InvalidStateTransition(self._state, SessionState.ROLLING_BACK)

        irreversible = self._side_effect_journal.get_irreversible_after_round(target_round)
        if irreversible:
            raise IrreversibleSideEffectError(irreversible)

        self._transition_to(SessionState.ROLLING_BACK)
        compensated = self._side_effect_journal.compensate_reversible_after_round(target_round)

        checkpoint = self._checkpoint_store.load(self.session_id, target_round)
        if checkpoint is None:
            self._transition_to(SessionState.PAUSED)
            raise RollbackFailed(self.session_id, target_round, "Checkpoint not found")

        self._memory.restore_snapshot(checkpoint.memory_snapshot)
        self._round_results = [RoundResult.deserialize(x) for x in checkpoint.round_results]
        self._directive_injector.restore_directives(checkpoint.active_directives)
        self._auto_pause_evaluator.restore_history(checkpoint.auto_pause_history)
        self._side_effect_journal = SideEffectJournal.deserialize(checkpoint.side_effect_journal)
        self._engine.restore_state(checkpoint.engine_state)
        self._current_round = target_round
        self._transition_to(SessionState.PAUSED)

        return {
            "status": "rolled_back",
            "target_round": target_round,
            "compensated_actions": compensated,
            "warning": None,
        }

    def _handle_cancel(self) -> dict:
        if self._state in (SessionState.COMPLETED, SessionState.CANCELLED):
            return {"status": "already_terminal"}
        self._state = SessionState.CANCELLED
        self._pause_event.set()
        self._feedback_event.set()
        return {"status": "cancelled"}

    def _debate_loop(self) -> None:
        try:
            for round_num in range(self._current_round + 1, self.config.max_rounds + 1):
                if self._state == SessionState.CANCELLED:
                    return

                self._cooperative_check(InterruptionPoint.ROUND_BOUNDARY)
                self._process_pending_commands()
                if self._state == SessionState.CANCELLED:
                    return

                self._current_round = round_num
                self._store_checkpoint(metadata={"kind": "pre_round"})

                result = self._execute_round(round_num)
                self._round_results.append(result)
                self._store_checkpoint(metadata={"kind": "post_round"})

                if self.config.mode == ExecutionMode.SEMI_INTERACTIVE:
                    reason = self._auto_pause_evaluator.evaluate(result, self._round_results, self._side_effect_journal)
                    if reason:
                        self._last_auto_pause_reason = reason
                        self._transition_to(SessionState.AWAITING_FEEDBACK)
                        self._pause_event.clear()
                        got_feedback = self._feedback_event.wait(timeout=self.config.feedback_timeout_seconds)
                        self._feedback_event.clear()
                        if not got_feedback:
                            self._auto_pause_evaluator.record_timeout_resume(self._current_round)
                            self._transition_to(SessionState.RUNNING)
                            self._pause_event.set()
                        elif self._state == SessionState.AWAITING_FEEDBACK:
                            self._transition_to(SessionState.PAUSED)
                            self._pause_event.wait()

            self._transition_to(SessionState.COMPLETED)
        except Exception:
            self._state = SessionState.FAILED
            raise

    def _execute_round(self, round_num: int) -> RoundResult:
        phases = ["thesis", "antithesis", "synthesis", "critic", "scoring"]
        phase_results: dict[str, Any] = {}
        phase_durations: dict[str, float] = {}

        for phase in phases:
            self._cooperative_check(InterruptionPoint.PHASE_BOUNDARY)
            self._process_pending_commands()
            if self._state == SessionState.CANCELLED:
                break

            prompt = self._directive_injector.build_phase_prompt(
                phase=phase,
                round_num=round_num,
                base_context=self._engine.get_phase_context(phase, round_num),
            )

            started = time.monotonic()
            result = self._engine.execute_phase(
                phase=phase,
                prompt=prompt,
                cooperative_check=self._cooperative_check,
                side_effect_recorder=self._record_side_effect,
                round_num=round_num,
            )
            phase_durations[phase] = time.monotonic() - started
            phase_results[phase] = result

        return RoundResult(
            round_num=round_num,
            thesis=phase_results.get("thesis", ""),
            antithesis=phase_results.get("antithesis", ""),
            synthesis=phase_results.get("synthesis", ""),
            critic_scores=phase_results.get("critic", {}),
            final_score=phase_results.get("scoring", 0.0),
            convergence_delta=self._engine.compute_convergence_delta(self._round_results, phase_results),
            side_effects=[e.serialize() for e in self._side_effect_journal.get_entries_for_round(round_num)],
            human_directives_applied=self._directive_injector.get_applied_keys(round_num),
            duration_seconds=sum(phase_durations.values()),
            phase_durations=phase_durations,
        )

    def _cooperative_check(self, point: InterruptionPoint) -> None:
        if self._state == SessionState.CANCELLED:
            return
        if self._state == SessionState.PAUSE_REQUESTED and self.config.pause_policy.can_pause_at(point):
            self._transition_to(SessionState.PAUSED)
            self._pause_acknowledged.set()
            self._pause_event.wait()
            self._pause_acknowledged.clear()

    def _record_side_effect(
        self,
        round_num: int,
        phase: str,
        tool_name: str,
        tool_args: dict,
        result: str,
        effect_type: SideEffectType,
        compensating_action: str | None = None,
    ) -> None:
        self._side_effect_journal.record(
            SideEffectEntry(
                round_num=round_num,
                phase=phase,
                effect_type=effect_type,
                tool_name=tool_name,
                tool_args=tool_args,
                result_summary=result[:500],
                compensating_action=compensating_action,
            )
        )

    def _store_checkpoint(self, metadata: dict | None = None) -> None:
        checkpoint = Checkpoint(
            checkpoint_id=f"ckpt_{uuid.uuid4().hex[:12]}",
            session_id=self.session_id,
            round_num=self._current_round,
            state=self._state,
            memory_snapshot=self._memory.export_snapshot(),
            round_results=[r.serialize() for r in self._round_results],
            active_directives=self._directive_injector.export_active_directives(),
            auto_pause_history=self._auto_pause_evaluator.export_history(),
            side_effect_journal=self._side_effect_journal.serialize(),
            engine_state=self._engine.export_state(),
            metadata=metadata or {},
        )
        self._checkpoint_store.save(checkpoint)

    def get_status(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self._state.value,
            "current_round": self._current_round,
            "max_rounds": self.config.max_rounds,
            "mode": self.config.mode.value,
            "available_checkpoints": self._checkpoint_store.list_checkpoints(self.session_id),
            "pending_commands": len(self._command_queue),
            "auto_pause_reason": self._last_auto_pause_reason,
            "round_results_summary": [
                {
                    "round": r.round_num,
                    "score": r.final_score,
                    "convergence": r.convergence_delta,
                }
                for r in self._round_results
            ],
            "created_at": self._created_at,
            "updated_at": self._updated_at,
        }


### 1.12 설계 포인트
- 핵심은 `threading.Event` 자체보다, `Event + cooperative interruption point + command queue` 조합이다.
- `pause()`는 즉시 정지 요청만 하고, 실제 정지는 safe point에서 일어난다.
- `resume()`는 `PAUSED`, `AWAITING_FEEDBACK`에서만 허용한다.
- `rollback()`은 반드시 `PAUSED` 상태에서만 허용해 중간 phase 복원을 금지한다.
- checkpoint는 pre-round, post-round를 둘 다 남길 수 있지만, 최소 요구는 라운드 경계 restore 보장이다.

---

## 2. human_directive 주입 메커니즘

### 2.1 핵심 원칙
- `human_directive`는 critic feedback의 덧붙임이 아니라 `CompactMemory`와 동급의 1급 상태다.
- prompt 조합은 단순 append가 아니라 우선순위 merge다.
- 우선순위는 `SYSTEM > HUMAN > CRITIC > SYNTHESIS > AMBIENT`다.
- 사용자가 명시적으로 override하면 critic guidance를 suppress할 수 있다.
- directive는 phase, round 범위, 내용 필터 기준으로 focus를 제한할 수 있다.

### 2.2 Prompt Injection 계층

text
[SYSTEM]
  고정 시스템 규칙, 안전 정책, task invariant
[HUMAN]
  사용자가 제출한 directive, focus constraint 적용
[CRITIC]
  critic loop 결과, 단 human override 존재 시 약화/제거 가능
[SYNTHESIS]
  이전 라운드 synthesis summary
[AMBIENT]
  일반 컨텍스트, long-tail memory, 로그


### 2.3 CompactMemory 통합 방식
- `CompactMemory.add_layer("human_directive:<id>", content, priority=DirectivePriority.HUMAN.value)` 형태로 저장한다.
- active directive 목록은 session runtime에도 유지하고, memory snapshot에도 포함한다.
- rollback 시 directive도 checkpoint 기준으로 같이 복원한다.
- memory export/import에 directive metadata가 같이 직렬화되어야 한다.

### 2.4 DirectiveInjector 설계

python
class DirectiveInjector:
    def __init__(self, memory: "CompactMemory"):
        self._memory = memory
        self._directives: list[HumanDirective] = []
        self._applied_log: dict[int, list[str]] = {}

    def inject(self, directive: HumanDirective, current_round: int) -> None:
        applies_from = directive.applies_from_round(current_round)
        if directive.focus_constraint and directive.focus_constraint.max_rounds:
            directive.focus_constraint.applies_until_round = applies_from + directive.focus_constraint.max_rounds - 1
        self._directives.append(directive)
        self._memory.add_layer(
            key=f"human_directive:{directive.idempotency_key}",
            content=directive.content,
            priority=directive.priority.value,
        )

    def build_phase_prompt(self, phase: str, round_num: int, base_context: str) -> str:
        ordered = self._resolve_applicable_directives(phase, round_num)
        merged = self._render_prompt(base_context, ordered)
        self._applied_log.setdefault(round_num, []).extend([d.idempotency_key for d in ordered])
        return merged

    def _resolve_applicable_directives(self, phase: str, round_num: int) -> list[HumanDirective]:
        active = []
        for directive in self._directives:
            if directive.applies_from_round(round_num - 1) > round_num:
                continue
            if directive.focus_constraint and not directive.focus_constraint.is_active_for(phase, round_num):
                continue
            active.append(directive)
        return self._apply_priority_rules(active)

    def _apply_priority_rules(self, directives: list[HumanDirective]) -> list[HumanDirective]:
        sorted_directives = sorted(directives, key=lambda d: d.priority.value, reverse=True)
        human = [d for d in sorted_directives if d.priority == DirectivePriority.HUMAN]
        others = [d for d in sorted_directives if d.priority != DirectivePriority.HUMAN]

        has_override = any(
            any(token in d.content.lower() for token in ["ignore critic", "override", "무시", "대신"])
            for d in human
        )
        if has_override:
            others = [d for d in others if d.priority != DirectivePriority.CRITIC]
        return human + others

    def _render_prompt(self, base_context: str, directives: list[HumanDirective]) -> str:
        sections = [base_context]
        if directives:
            sections.append("\n[Human / Priority Guidance]")
            for directive in directives:
                sections.append(f"- ({directive.priority.name}) {directive.content}")
        return "\n".join(sections)

    def get_applied_keys(self, round_num: int) -> list[str]:
        return self._applied_log.get(round_num, [])

    def export_active_directives(self) -> list[dict]:
        return [d.serialize() for d in self._directives]

    def restore_directives(self, data: list[dict]) -> None:
        self._directives = [HumanDirective.deserialize(x) for x in data]

    def truncate_applied_log(self, after_round: int) -> None:
        self._applied_log = {r: v for r, v in self._applied_log.items() if r <= after_round}


### 2.5 Focus Constraint 규칙
- `target_phases`: 특정 phase에만 directive 적용.
- `max_rounds`: 지정 라운드 수 이후 자동 만료.
- `content_filter`: 특정 주제에만 relevance gate로 사용 가능.
- `target_round`: 현재 라운드 중간 제출이라도 기본값은 다음 라운드부터 적용.
- 현재 실행 중 phase prompt는 이미 확정되었으므로, mid-phase feedback은 절대 현재 phase에 반영하지 않는다.

---

## 3. 3가지 실행 모드

### 3.1 모드 비교

| 항목 | BATCH | INTERACTIVE | SEMI_INTERACTIVE |
|------|-------|-------------|------------------|
| Pause 가능 | ✗ | ✓ (수동) | ✓ (자동 + 수동) |
| Feedback 주입 | ✗ | ✓ | ✓ |
| Checkpoint 생성 | 최종 1회 또는 선택적 | 매 라운드 | 매 라운드 |
| Rollback | ✗ | ✓ | ✓ |
| Command Queue | ✗ | ✓ | ✓ |
| Auto-pause | ✗ | ✗ | ✓ |

### 3.2 모드별 의미
- `batch`: 기존 legacy 동작. 세션 제어 오버헤드 없이 끝까지 실행한다.
- `interactive`: 사용자가 필요할 때 수동으로 pause/resume/feedback/rollback 한다.
- `semi_interactive`: 자동 감지 규칙으로 “개입 가치가 높은 순간”에 pause를 건다.

### 3.3 Semi-Interactive Auto-Pause 조건 5가지
문서 전반의 표현을 다음 5개 기준으로 통일한다.
- `score divergence`: 이전 라운드 대비 최종 점수 하락폭이 임계값 이상.
- `consensus stall`: 최근 N개 라운드에서 수렴 진전이 거의 없음.
- `confidence drop`: critic spread 또는 confidence proxy가 급격히 악화됨.
- `cost exceeded`: 누적 비용 또는 budget 사용률이 임계값 초과.
- `critic severity`: critic severity score 또는 irreversible risk 신호가 임계 이상.

기존 초안의 `score_drop`, `convergence_stall`, `critic_disagreement`, `irreversible_action`, `cost_threshold`는 구현 레벨에서 위 개념에 각각 매핑된다.

### 3.4 AutoPauseEvaluator

python
class AutoPauseEvaluator:
    def __init__(self, config: AutoPauseConfig):
        self._config = config
        self._history: list[dict] = []

    def evaluate(
        self,
        current: RoundResult,
        all_results: list[RoundResult],
        side_effects: SideEffectJournal,
    ) -> Optional[str]:
        reason = None

        if self._config.on_score_drop and len(all_results) >= 2:
            prev_score = all_results[-2].final_score
            drop = prev_score - current.final_score
            if drop >= self._config.score_drop_threshold:
                reason = f"score_divergence: {prev_score:.2f} -> {current.final_score:.2f} (delta={drop:.2f})"

        if self._config.on_convergence_stall and not reason:
            n = self._config.convergence_stall_rounds
            if len(all_results) >= n:
                recent = all_results[-n:]
                if all(abs(r.convergence_delta) < 0.01 for r in recent):
                    reason = f"consensus_stall: {n} rounds with delta < 0.01"

        if self._config.on_critic_disagreement and not reason:
            scores = list(current.critic_scores.values())
            if len(scores) >= 2:
                spread = max(scores) - min(scores)
                if spread >= self._config.critic_disagreement_threshold:
                    reason = f"confidence_drop: critic spread={spread:.2f}"

        if self._config.on_irreversible_action and not reason:
            irreversible = [
                e for e in side_effects.get_entries_for_round(current.round_num)
                if e.effect_type == SideEffectType.IRREVERSIBLE
            ]
            if irreversible:
                reason = f"critic_severity: irreversible actions={[e.tool_name for e in irreversible]}"

        if self._config.on_cost_threshold and not reason:
            total_duration = sum(r.duration_seconds for r in all_results)
            max_duration = len(all_results) * 60
            if max_duration > 0:
                usage_pct = (total_duration / max_duration) * 100
                if usage_pct >= self._config.cost_threshold_percent:
                    reason = f"cost_exceeded: usage={usage_pct:.1f}%"

        self._history.append({
            "round": current.round_num,
            "triggered": reason is not None,
            "reason": reason,
            "timestamp": time.time(),
        })
        return reason

    def record_timeout_resume(self, round_num: int) -> None:
        self._history.append({
            "round": round_num,
            "triggered": False,
            "reason": "timeout_resume",
            "timestamp": time.time(),
        })

    def truncate_to_round(self, round_num: int) -> None:
        self._history = [h for h in self._history if h["round"] <= round_num]

    def export_history(self) -> list[dict]:
        return list(self._history)

    def restore_history(self, history: list[dict]) -> None:
        self._history = list(history)


---

## 4. REST API 설계

### 4.1 `POST /api/horcrux/session`
세션 생성과 제어를 하나의 action-based 엔드포인트로 묶는다.

#### Request: create


{
  "action": "create",
  "task": "Analyze the security implications of ...",
  "config": {
    "mode": "semi_interactive",
    "max_rounds": 10,
    "pause_policy": {
      "allowed_points": ["round_boundary", "phase_boundary"],
      "tool_call_timeout_ms": 30000,
      "llm_call_cancel_policy": "wait",
      "force_pause_after_ms": 60000
    },
    "auto_pause": {
      "on_score_drop": true,
      "score_drop_threshold": 0.5,
      "on_convergence_stall": true,
      "convergence_stall_rounds": 3,
      "on_critic_disagreement": true,
      "critic_disagreement_threshold": 2.0,
      "on_irreversible_action": true,
      "on_cost_threshold": true,
      "cost_threshold_percent": 80.0
    },
    "checkpoint_retention": 10,
    "feedback_timeout_seconds": 300,
    "side_effect_policy": "warn_on_irreversible"
  },
  "models": ["claude-sonnet-4-6", "gemini-2.5-pro"],
  "critic_models": ["claude-opus-4-6"]
}


#### Response: `201 Created`


{
  "session_id": "sess_a1b2c3d4",
  "state": "running",
  "mode": "semi_interactive",
  "created_at": "2026-03-27T10:00:00Z",
  "config": {
    "mode": "semi_interactive",
    "max_rounds": 10,
    "pause_policy": {
      "allowed_points": ["round_boundary", "phase_boundary"],
      "tool_call_timeout_ms": 30000,
      "llm_call_cancel_policy": "wait",
      "force_pause_after_ms": 60000
    },
    "auto_pause": {
      "on_score_drop": true,
      "score_drop_threshold": 0.5,
      "on_convergence_stall": true,
      "convergence_stall_rounds": 3,
      "on_critic_disagreement": true,
      "critic_disagreement_threshold": 2.0,
      "on_irreversible_action": true,
      "on_cost_threshold": true,
      "cost_threshold_percent": 80.0
    },
    "checkpoint_retention": 10,
    "checkpoint_dir": "./checkpoints",
    "feedback_timeout_seconds": 300,
    "command_queue_max_size": 100,
    "side_effect_policy": "warn_on_irreversible"
  }
}


#### Request: pause


{
  "action": "pause",
  "session_id": "sess_a1b2c3d4",
  "idempotency_key": "pause_001"
}


#### Response: `200 OK`


{
  "session_id": "sess_a1b2c3d4",
  "state": "pause_requested",
  "current_round": 3,
  "message": "Pause requested. Will pause at next cooperative check point."
}


#### Request: resume


{
  "action": "resume",
  "session_id": "sess_a1b2c3d4",
  "idempotency_key": "resume_001"
}


#### Request: rollback


{
  "action": "rollback",
  "session_id": "sess_a1b2c3d4",
  "target_round": 2,
  "idempotency_key": "rollback_001"
}


#### Response: rollback success


{
  "session_id": "sess_a1b2c3d4",
  "state": "paused",
  "current_round": 2,
  "rollback": {
    "from_round": 5,
    "to_round": 2,
    "compensated_actions": ["delete_file:/tmp/output.txt"],
    "irreversible_warning": null
  }
}


#### Response: rollback blocked


{
  "error": "rollback_blocked",
  "message": "Cannot rollback: irreversible side effects exist after round 2",
  "irreversible_effects": [
    {
      "round_num": 4,
      "phase": "synthesis",
      "tool_name": "send_slack_message",
      "result_summary": "Message sent to #general"
    }
  ],
  "available_rollback_targets": [4, 5]
}


#### Request: status


{
  "action": "status",
  "session_id": "sess_a1b2c3d4"
}


#### Response: status


{
  "session_id": "sess_a1b2c3d4",
  "state": "paused",
  "current_round": 3,
  "max_rounds": 10,
  "mode": "semi_interactive",
  "available_checkpoints": [1, 2, 3],
  "pending_commands": 0,
  "auto_pause_reason": "confidence_drop: critic spread=2.5",
  "round_results_summary": [
    {"round": 1, "score": 3.5, "convergence": 0.0},
    {"round": 2, "score": 4.0, "convergence": 0.5},
    {"round": 3, "score": 3.2, "convergence": -0.8}
  ]
}


### 4.2 `POST /api/horcrux/feedback`

#### Request


{
  "session_id": "sess_a1b2c3d4",
  "directive": {
    "content": "synthesis에서 보안 관점을 더 강조해줘. 성능 trade-off 분석도 포함.",
    "priority": 100,
    "target_round": null,
    "focus_constraint": {
      "target_phases": ["synthesis", "antithesis"],
      "max_rounds": 3,
      "content_filter": "security|보안"
    }
  },
  "idempotency_key": "fb_001",
  "auto_resume": true
}


#### Response


{
  "status": "accepted",
  "directive_id": "fb_001",
  "applies_from_round": 4,
  "active_directives_count": 2,
  "session_state": "running",
  "message": "Directive accepted. Session auto-resumed."
}


### 4.3 Error Responses


{"error": "session_not_found", "session_id": "sess_invalid"}



{"error": "invalid_state", "current_state": "completed", "message": "Cannot submit feedback to completed session"}



{"error": "queue_full", "session_id": "sess_a1b2c3d4", "queue_size": 100}



{"error": "validation_error", "details": [{"field": "directive.priority", "message": "Must be 10, 50, or 100"}]}


### 4.4 Server 통합 코드

python
from aiohttp import web


class SessionManager:
    """Active session 관리와 crash recovery 담당"""

    def __init__(self, checkpoint_base_dir: Path):
        self._sessions: dict[str, InteractiveSession] = {}
        self._checkpoint_base_dir = checkpoint_base_dir
        self._meta_file = checkpoint_base_dir / "sessions_meta.json"

    def create_session(
        self,
        task: str,
        config: SessionConfig,
        engine: "HorcruxDebateEngine",
        memory: "CompactMemory",
    ) -> InteractiveSession:
        session_id = f"sess_{uuid.uuid4().hex[:8]}"
        session = InteractiveSession(session_id, config, engine, memory)
        self._sessions[session_id] = session
        self._save_session_meta()
        return session

    def get_session(self, session_id: str) -> InteractiveSession:
        if session_id not in self._sessions:
            raise SessionNotFound(session_id)
        return self._sessions[session_id]

    def recover_sessions(self) -> list[str]:
        recovered: list[str] = []
        if not self._meta_file.exists():
            return recovered

        meta = json.loads(self._meta_file.read_text(encoding="utf-8"))
        for entry in meta.get("sessions", []):
            sid = entry["session_id"]
            if entry["state"] in ("completed", "cancelled", "failed"):
                continue

            store = CheckpointStore(self._checkpoint_base_dir)
            rounds = store.list_checkpoints(sid)
            if not rounds:
                continue

            latest = store.load(sid, rounds[-1])
            if latest is None:
                continue

            config = SessionConfig.deserialize(entry["config"])
            engine = self._restore_engine_from_checkpoint(latest)
            memory = self._restore_memory_from_checkpoint(latest)
            session = InteractiveSession(sid, config, engine, memory)
            session._current_round = latest.round_num
            session._state = SessionState.PAUSED
            session._round_results = [RoundResult.deserialize(x) for x in latest.round_results]
            session._directive_injector.restore_directives(latest.active_directives)
            session._auto_pause_evaluator.restore_history(latest.auto_pause_history)
            session._side_effect_journal = SideEffectJournal.deserialize(latest.side_effect_journal)
            self._sessions[sid] = session
            recovered.append(sid)

        return recovered

    def _restore_engine_from_checkpoint(self, checkpoint: Checkpoint) -> "HorcruxDebateEngine":
        engine = HorcruxDebateEngine()
        engine.restore_state(checkpoint.engine_state)
        return engine

    def _restore_memory_from_checkpoint(self, checkpoint: Checkpoint) -> "CompactMemory":
        memory = CompactMemory()
        memory.restore_snapshot(checkpoint.memory_snapshot)
        return memory

    def _save_session_meta(self) -> None:
        meta = {
            "sessions": [
                {
                    "session_id": sid,
                    "state": session.state.value,
                    "config": session.config.serialize(),
                    "current_round": session.current_round,
                }
                for sid, session in self._sessions.items()
            ]
        }
        tmp = self._meta_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(self._meta_file))


async def handle_session(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["session_manager"]
    body = await request.json()
    action = body.get("action")

    try:
        if action == "create":
            config = SessionConfig.deserialize(body.get("config", {}))
            engine = request.app["engine_factory"](body)
            memory = CompactMemory()
            session = manager.create_session(body["task"], config, engine, memory)
            session.start()
            return web.json_response({
                "session_id": session.session_id,
                "state": session.state.value,
                "mode": config.mode.value,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "config": config.serialize(),
            }, status=201)

        session_id = body["session_id"]
        session = manager.get_session(session_id)

        if action == "pause":
            result = session.pause(idempotency_key=body.get("idempotency_key")).result(timeout=5.0)
            return web.json_response({"session_id": session_id, **result})

        if action == "resume":
            result = session.resume(idempotency_key=body.get("idempotency_key")).result(timeout=5.0)
            return web.json_response({"session_id": session_id, **result})

        if action == "rollback":
            try:
                result = session.rollback(
                    target_round=body["target_round"],
                    idempotency_key=body.get("idempotency_key"),
                ).result(timeout=30.0)
                return web.json_response({"session_id": session_id, **result})
            except IrreversibleSideEffectError as exc:
                return web.json_response({
                    "error": "rollback_blocked",
                    "message": str(exc),
                    "irreversible_effects": [x.serialize() for x in exc.effects],
                    "available_rollback_targets": session._checkpoint_store.list_checkpoints(session_id),
                }, status=409)

        if action == "status":
            return web.json_response(session.get_status())

        if action == "cancel":
            result = session.cancel(idempotency_key=body.get("idempotency_key")).result(timeout=5.0)
            return web.json_response({"session_id": session_id, **result})

        return web.json_response({"error": "unknown_action", "action": action}, status=400)

    except SessionNotFound:
        return web.json_response({"error": "session_not_found", "session_id": body.get("session_id")}, status=404)
    except InvalidStateTransition as exc:
        return web.json_response({
            "error": "invalid_state",
            "current_state": exc.current.value,
            "attempted": exc.attempted.value,
        }, status=409)
    except CommandQueueFull:
        return web.json_response({"error": "queue_full", "session_id": body.get("session_id")}, status=429)
    except Exception as exc:
        return web.json_response({"error": "internal", "message": str(exc)}, status=500)


async def handle_feedback(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["session_manager"]
    body = await request.json()

    try:
        session = manager.get_session(body["session_id"])
        directive_payload = body["directive"]
        directive = HumanDirective(
            content=directive_payload["content"],
            priority=DirectivePriority(directive_payload.get("priority", 100)),
            target_round=directive_payload.get("target_round"),
            focus_constraint=FocusConstraint(**directive_payload["focus_constraint"]) if directive_payload.get("focus_constraint") else None,
            idempotency_key=body.get("idempotency_key", str(uuid.uuid4())),
        )

        result = session.submit_feedback(directive, idempotency_key=directive.idempotency_key).result(timeout=5.0)

        if body.get("auto_resume") and session.state in (SessionState.PAUSED, SessionState.AWAITING_FEEDBACK):
            session.resume().result(timeout=5.0)

        return web.json_response({
            **result,
            "session_state": session.state.value,
            "active_directives_count": len(session._directive_injector.export_active_directives()),
        })

    except SessionNotFound:
        return web.json_response({"error": "session_not_found"}, status=404)
    except InvalidStateTransition as exc:
        return web.json_response({"error": "invalid_state", "current_state": exc.current.value}, status=409)
    except CommandQueueFull:
        return web.json_response({"error": "queue_full"}, status=429)


def setup_interactive_routes(app: web.Application) -> None:
    app.router.add_post("/api/horcrux/session", handle_session)
    app.router.add_post("/api/horcrux/feedback", handle_feedback)


---

## 5. MCP 도구 설계

### 5.1 `horcrux_feedback` 도구

python
HORCRUX_FEEDBACK_TOOL = {
    "name": "horcrux_feedback",
    "description": (
        "Submit feedback/directive to an active Horcrux interactive debate session. "
        "The directive is injected with human priority and can optionally auto-resume the session."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Active session ID (e.g., sess_a1b2c3d4)"
            },
            "content": {
                "type": "string",
                "description": "Feedback content to inject as human directive"
            },
            "target_phases": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["thesis", "antithesis", "synthesis", "critic", "scoring"]
                },
                "description": "Optional phase filter"
            },
            "max_rounds": {
                "type": "integer",
                "minimum": 1,
                "description": "Number of rounds this directive remains active"
            },
            "auto_resume": {
                "type": "boolean",
                "default": false,
                "description": "Resume paused session after feedback submission"
            }
        },
        "required": ["session_id", "content"]
    }
}


### 5.2 `horcrux_session` 도구

python
HORCRUX_SESSION_TOOL = {
    "name": "horcrux_session",
    "description": "Control an interactive Horcrux session: pause, resume, rollback, status, cancel.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["pause", "resume", "rollback", "status", "cancel"]
            },
            "session_id": {
                "type": "string"
            },
            "target_round": {
                "type": "integer",
                "minimum": 0,
                "description": "Required for rollback"
            }
        },
        "required": ["action", "session_id"]
    }
}


### 5.3 `horcrux_run`에 interactive 파라미터 추가

python
HORCRUX_RUN_TOOL_UPDATED = {
    "name": "horcrux_run",
    "description": "Run a Horcrux multi-model debate in batch, interactive, or semi-interactive mode.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Debate topic or task"},
            "rounds": {"type": "integer", "default": 5},
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Debate participant models"
            },
            "interactive": {
                "type": "string",
                "enum": ["batch", "interactive", "semi_interactive"],
                "default": "batch",
                "description": (
                    "Execution mode. batch=no pause. interactive=manual pause/resume/feedback. "
                    "semi_interactive=auto-pause on score divergence, consensus stall, confidence drop, "
                    "cost exceeded, or critic severity."
                )
            },
            "auto_pause_config": {
                "type": "object",
                "properties": {
                    "on_score_drop": {"type": "boolean", "default": true},
                    "score_drop_threshold": {"type": "number", "default": 0.5},
                    "on_convergence_stall": {"type": "boolean", "default": true},
                    "convergence_stall_rounds": {"type": "integer", "default": 3},
                    "on_critic_disagreement": {"type": "boolean", "default": true},
                    "critic_disagreement_threshold": {"type": "number", "default": 2.0},
                    "on_irreversible_action": {"type": "boolean", "default": true},
                    "on_cost_threshold": {"type": "boolean", "default": true},
                    "cost_threshold_percent": {"type": "number", "default": 80.0}
                }
            },
            "side_effect_policy": {
                "type": "string",
                "enum": ["block", "warn_on_irreversible", "allow_all"],
                "default": "warn_on_irreversible"
            }
        },
        "required": ["task"]
    }
}


### 5.4 MCP 핸들러

python
async def handle_horcrux_feedback(args: dict) -> dict:
    manager = get_session_manager()
    session = manager.get_session(args["session_id"])

    focus = None
    if args.get("target_phases") or args.get("max_rounds"):
        focus = FocusConstraint(
            target_phases=args.get("target_phases"),
            max_rounds=args.get("max_rounds"),
        )

    directive = HumanDirective(
        content=args["content"],
        priority=DirectivePriority.HUMAN,
        focus_constraint=focus,
    )

    result = session.submit_feedback(directive).result(timeout=10.0)
    if args.get("auto_resume") and session.state in (SessionState.PAUSED, SessionState.AWAITING_FEEDBACK):
        session.resume().result(timeout=5.0)

    return {
        "content": [{
            "type": "text",
            "text": json.dumps({
                "status": "accepted",
                "directive_id": directive.idempotency_key,
                "session_state": session.state.value,
                "applies_from_round": result.get("applies_from_round"),
            }, indent=2)
        }]
    }


async def handle_horcrux_session(args: dict) -> dict:
    manager = get_session_manager()
    session = manager.get_session(args["session_id"])
    action = args["action"]

    if action == "status":
        return {"content": [{"type": "text", "text": json.dumps(session.get_status(), indent=2)}]}

    action_map = {
        "pause": lambda: session.pause(),
        "resume": lambda: session.resume(),
        "cancel": lambda: session.cancel(),
        "rollback": lambda: session.rollback(args["target_round"]),
    }

    result = action_map[action]().result(timeout=30.0)
    return {
        "content": [{
            "type": "text",
            "text": json.dumps({"session_id": args["session_id"], **result}, indent=2)
        }]
    }


---

## 6. Rollback 메커니즘

### 6.1 판단 흐름

text
rollback(target_round=N)
    │
    ▼
[1] 현재 state == PAUSED ?
    ├─ No  -> InvalidStateTransition
    └─ Yes
[2] Checkpoint(round=N) 존재 ?
    ├─ No  -> RollbackFailed
    └─ Yes
[3] round N+1 ~ current 사이에 irreversible side-effect 존재 ?
    ├─ Yes -> IrreversibleSideEffectError
    └─ No
[4] reversible side-effect 보상 실행 (역순)
[5] checkpoint 복원
[6] state -> PAUSED, current_round = N


### 6.2 Side-Effect 분류 정책

python
SIDE_EFFECT_REGISTRY: dict[str, SideEffectType] = {
    "web_search": SideEffectType.IDEMPOTENT,
    "web_fetch": SideEffectType.IDEMPOTENT,
    "read_file": SideEffectType.IDEMPOTENT,
    "glob": SideEffectType.IDEMPOTENT,
    "grep": SideEffectType.IDEMPOTENT,
    "write_file": SideEffectType.REVERSIBLE,
    "edit_file": SideEffectType.REVERSIBLE,
    "create_directory": SideEffectType.REVERSIBLE,
    "send_slack_message": SideEffectType.IRREVERSIBLE,
    "send_email": SideEffectType.IRREVERSIBLE,
    "http_post": SideEffectType.IRREVERSIBLE,
    "git_push": SideEffectType.IRREVERSIBLE,
    "delete_file": SideEffectType.IRREVERSIBLE,
}


def classify_side_effect(tool_name: str) -> SideEffectType:
    return SIDE_EFFECT_REGISTRY.get(tool_name, SideEffectType.IRREVERSIBLE)


### 6.3 Rollback 시 복원 대상
- `CompactMemory.restore_snapshot()`
- `DirectiveInjector.restore_directives()`
- `AutoPauseEvaluator.restore_history()` 또는 truncate
- `SideEffectJournal.truncate_to_round()`
- `HorcruxDebateEngine.restore_state()`
- `round_results` 절단
- `current_round` 조정

### 6.4 Rollback과 Directive 상호작용
- checkpoint 시점에 활성 directive만 복원한다.
- rollback 이후 제출되는 새 feedback은 기본적으로 `current_round + 1`부터 적용한다.
- 특정 구현에서는 `applies_after_checkpoint` 메타데이터를 둘 수 있다.
- rollback이 directive 적용 로그까지 복원해야, “이미 적용된 directive가 다시 중복 반영되는 문제”를 막을 수 있다.

---

## 7. 에러 핸들링

### 7.1 Pause 중 서버 재시작

python
class ServerRecovery:
    @staticmethod
    def recover(manager: SessionManager) -> dict:
        recovered = manager.recover_sessions()
        return {
            "recovered_sessions": recovered,
            "action_required": "Sessions restored in PAUSED state. Client must explicitly resume.",
        }


복구 프로토콜:
1. `sessions_meta.json`에서 비정상 종료 세션 식별.
2. 마지막 checkpoint 로드.
3. 세션을 `PAUSED` 상태로 재생성.
4. 자동 resume는 하지 않는다.
5. 클라이언트가 status 조회 후 명시적으로 resume 한다.

### 7.2 Feedback Timeout

text
SEMI_INTERACTIVE auto-pause
    │
    ▼
AWAITING_FEEDBACK
    │
    ├─ timeout 내 feedback 수신
    │   -> directive 적용
    │   -> auto_resume=true 이면 RUNNING
    │   -> 아니면 PAUSED 유지
    │
    └─ timeout 초과
        -> timeout_resume 이벤트 기록
        -> 기존 directive 유지
        -> 자동 RUNNING 복귀


### 7.3 Feedback 순서 꼬임 방지: 4중 안전장치
1. `Command Queue` 직렬화.
2. `sequence_num` 단조 증가.
3. `idempotency_key` 중복 제거.
4. `target_round` 기반 라운드 경계 적용.

### 7.4 Partial Checkpoint Write 복구
- `.tmp` 파일에 먼저 쓰고 `os.replace()`로 atomic rename.
- parse 실패 checkpoint는 corrupt로 간주하고 무시한다.
- `list_checkpoints()`는 load 가능한 파일만 노출하도록 개선할 수 있다.
- 운영 로그에 corrupt round를 남겨 postmortem 가능하게 한다.

### 7.5 추가 예외 매핑
- `404`: session 없음.
- `409`: invalid state, rollback blocked.
- `422`: directive schema 검증 실패.
- `429`: command queue full.
- `500`: 내부 오류.

---

## 8. 기존 코드 통합용 인터페이스

python
class CompactMemory:
    def add_layer(self, key: str, content: str, priority: int = 0) -> None:
        ...

    def export_snapshot(self) -> dict:
        ...

    def restore_snapshot(self, snapshot: dict) -> None:
        ...


class HorcruxDebateEngine:
    def get_phase_context(self, phase: str, round_num: int) -> str:
        ...

    def execute_phase(
        self,
        phase: str,
        prompt: str,
        cooperative_check: Callable[[InterruptionPoint], None],
        side_effect_recorder: Callable[..., None],
        round_num: int,
    ) -> Any:
        """
        - LLM 호출 전: cooperative_check(PRE_LLM_CALL)
        - Tool 호출 전: cooperative_check(PRE_TOOL_CALL)
        - Tool 호출 후: cooperative_check(POST_TOOL_CALL) + side_effect_recorder(...)
        """
        ...

    def compute_convergence_delta(self, prev_results: list[RoundResult], current_phases: dict) -> float:
        ...

    def export_state(self) -> dict:
        ...

    def restore_state(self, state: dict) -> None:
        ...


---

## 9. 기각된 대안

| 대안 | 기각 사유 |
|------|----------|
| asyncio 기반 전환 | 기존 동기 `HorcruxDebateEngine`과의 통합 복잡성이 크고 async/sync 경계 deadlock 위험이 있다. |
| 단순 `RLock` 기반 동시성 | pause → feedback → resume → rollback 순서 제어가 어렵고 lock 중첩으로 디버깅이 힘들다. |
| Pickle checkpoint | 보안 위험과 버전 호환성 문제가 있다. JSON이 안전하고 투명하다. |
| Full event-sourcing | 현재 요구에 비해 복잡도가 과도하다. checkpoint restore로 충분하다. |
| Rollback 시 irreversible 무시 | 운영 일관성을 깨뜨린다. 명시적 차단이 안전하다. |

---

## 10. 최종 설계 요약
- Horcrux Interactive Mode의 본체는 HTTP API나 UI가 아니라 `InteractiveSession` 런타임이다.
- durable session, checkpoint, rollback, recovery를 먼저 설계해야 pause/resume 기능이 실제 운영에서 안전해진다.
- `human_directive`는 메모리 계층에서 `CRITIC`보다 높은 1급 입력이며, focus constraint와 target round를 통해 정밀 제어된다.
- `semi_interactive`는 단순 타이머 멈춤이 아니라 score divergence, consensus stall, confidence drop, cost exceeded, critic severity를 감지하는 알고리즘 기반 개입 모드다.
- command queue, idempotency, checkpoint atomic write, side-effect journal이 함께 있어야 feedback 순서 꼬임과 rollback 오염을 막을 수 있다.
