"""
core/adaptive/compact_memory.py — Phase 1.5: Compact State Memory

전체 이전 출력을 매번 재삽입하지 않고, 압축된 상태 메모리로 대체.

Memory Layers (mode별 차등 적용):
  - working_memory: 현재 목표, blocker, 보존 항목 (모든 모드)
  - decision_memory: 수락/거부된 결정, 미해결 질문 (standard, full_horcrux)
  - result_summary_memory: 콘텐츠/구조 요약, 핵심 메시지 (standard, full_horcrux)

Mode Policy:
  - fast: working_memory only (or skip)
  - standard: working_memory + result_summary_memory
  - full_horcrux: all 3 layers

Round Checkpoint:
  - 매 라운드 후 요약 체크포인트 생성
  - 다음 라운드에 full output 대신 checkpoint만 전달

Delta-based Prompting:
  - 이전 전체 이슈 대신 new_blockers + resolved_items + preserve만 전달
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ═══════════════════════════════════════════
# Memory Layer Schemas
# ═══════════════════════════════════════════

@dataclass
class WorkingMemory:
    """현재 작업 상태 — 모든 모드에서 사용."""
    task: str = ""
    current_goal: str = ""
    blocking_issues: List[str] = field(default_factory=list)
    preserve: List[str] = field(default_factory=list)
    must_not_change: List[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        lines = [
            f"[WORKING MEMORY]",
            f"Goal: {self.current_goal}" if self.current_goal else "",
        ]
        if self.blocking_issues:
            lines.append(f"Blockers: {'; '.join(self.blocking_issues)}")
        if self.preserve:
            lines.append(f"Preserve: {'; '.join(self.preserve)}")
        if self.must_not_change:
            lines.append(f"Must NOT change: {'; '.join(self.must_not_change)}")
        return "\n".join(l for l in lines if l)

    def to_dict(self) -> dict:
        return {
            "task": self.task, "current_goal": self.current_goal,
            "blocking_issues": self.blocking_issues,
            "preserve": self.preserve, "must_not_change": self.must_not_change,
        }


@dataclass
class DecisionMemory:
    """수락/거부된 결정 추적 — standard, full_horcrux에서 사용."""
    accepted_decisions: List[Dict[str, str]] = field(default_factory=list)
    rejected_alternatives: List[Dict[str, str]] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        lines = ["[DECISION MEMORY]"]
        if self.accepted_decisions:
            for d in self.accepted_decisions[-3:]:  # 최근 3개만
                lines.append(f"  ✅ {d.get('topic', '')}: {d.get('choice', '')} ({d.get('reason', '')})")
        if self.open_questions:
            lines.append(f"  ❓ Open: {'; '.join(self.open_questions[:3])}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def to_dict(self) -> dict:
        return {
            "accepted_decisions": self.accepted_decisions,
            "rejected_alternatives": self.rejected_alternatives,
            "open_questions": self.open_questions,
        }


@dataclass
class ResultSummaryMemory:
    """결과 요약 — standard, full_horcrux에서 사용."""
    content_summary: str = ""
    structure_summary: str = ""
    key_messages: List[str] = field(default_factory=list)
    resolved_items: List[str] = field(default_factory=list)
    remaining_blockers: List[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        lines = ["[RESULT SUMMARY]"]
        if self.content_summary:
            lines.append(f"  Content: {self.content_summary}")
        if self.structure_summary:
            lines.append(f"  Structure: {self.structure_summary}")
        if self.remaining_blockers:
            lines.append(f"  Remaining: {'; '.join(self.remaining_blockers)}")
        if self.resolved_items:
            lines.append(f"  Resolved: {'; '.join(self.resolved_items[-3:])}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def to_dict(self) -> dict:
        return {
            "content_summary": self.content_summary,
            "structure_summary": self.structure_summary,
            "key_messages": self.key_messages,
            "resolved_items": self.resolved_items,
            "remaining_blockers": self.remaining_blockers,
        }


# ═══════════════════════════════════════════
# Round Checkpoint
# ═══════════════════════════════════════════

@dataclass
class RoundCheckpoint:
    """매 라운드 후 생성되는 요약 체크포인트."""
    round: int = 0
    score: float = 0.0
    current_conclusion: str = ""
    what_changed: List[str] = field(default_factory=list)
    remaining_blockers: List[str] = field(default_factory=list)
    preserve: List[str] = field(default_factory=list)
    must_not_change: List[str] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        lines = [
            f"[CHECKPOINT R{self.round}] Score: {self.score}/10",
        ]
        if self.current_conclusion:
            lines.append(f"  Conclusion: {self.current_conclusion[:200]}")
        if self.what_changed:
            lines.append(f"  Changed: {'; '.join(self.what_changed[:3])}")
        if self.remaining_blockers:
            lines.append(f"  Blockers: {'; '.join(self.remaining_blockers)}")
        if self.preserve:
            lines.append(f"  Preserve: {'; '.join(self.preserve[:3])}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "round": self.round, "score": self.score,
            "current_conclusion": self.current_conclusion,
            "what_changed": self.what_changed,
            "remaining_blockers": self.remaining_blockers,
            "preserve": self.preserve,
            "must_not_change": self.must_not_change,
        }


# ═══════════════════════════════════════════
# Delta Prompt
# ═══════════════════════════════════════════

@dataclass
class DeltaPrompt:
    """전체 재삽입 대신 변경분만 전달하는 프롬프트 구조."""
    current_state_summary: str = ""
    new_blockers: List[str] = field(default_factory=list)
    resolved_items: List[str] = field(default_factory=list)
    preserve: List[str] = field(default_factory=list)
    next_action: str = ""

    def to_prompt_block(self) -> str:
        lines = ["[DELTA UPDATE]"]
        if self.current_state_summary:
            lines.append(f"  State: {self.current_state_summary}")
        if self.new_blockers:
            lines.append(f"  🔴 New blockers: {'; '.join(self.new_blockers)}")
        if self.resolved_items:
            lines.append(f"  ✅ Resolved: {'; '.join(self.resolved_items)}")
        if self.preserve:
            lines.append(f"  🔒 Preserve: {'; '.join(self.preserve)}")
        if self.next_action:
            lines.append(f"  → Next: {self.next_action}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "current_state_summary": self.current_state_summary,
            "new_blockers": self.new_blockers,
            "resolved_items": self.resolved_items,
            "preserve": self.preserve,
            "next_action": self.next_action,
        }


# ═══════════════════════════════════════════
# Compact Memory Manager
# ═══════════════════════════════════════════

MEMORY_POLICY = {
    "fast": ["working"],
    "standard": ["working", "result_summary"],
    "full": ["working", "decision", "result_summary"],
    "full_horcrux": ["working", "decision", "result_summary"],  # R10: deprecated alias
}


class CompactMemory:
    """모드별 차등 메모리 관리자."""

    def __init__(self, mode: str, task: str):
        self.mode = mode
        self.working = WorkingMemory(task=task, current_goal=task[:200])
        self.decision = DecisionMemory()
        self.result_summary = ResultSummaryMemory()
        self.checkpoints: List[RoundCheckpoint] = []
        self._active_layers = MEMORY_POLICY.get(mode, ["working"])

    def update_from_critic(
        self,
        critic_text: str,
        score: float,
        round_num: int,
        solution_summary: str = "",
    ):
        """critic 결과로 메모리 업데이트."""
        # blockers 추출 (간단한 heuristic)
        new_blockers = []
        resolved = []

        for line in critic_text.split("\n"):
            line_lower = line.strip().lower()
            if any(kw in line_lower for kw in ["blocker", "critical", "must fix", "error", "bug"]):
                new_blockers.append(line.strip()[:100])
            elif any(kw in line_lower for kw in ["good", "correct", "well done", "resolved"]):
                resolved.append(line.strip()[:100])

        # Working memory 업데이트
        prev_blockers = self.working.blocking_issues[:]
        self.working.blocking_issues = new_blockers[:5]

        if resolved:
            for item in resolved[:3]:
                if item not in self.working.preserve:
                    self.working.preserve.append(item)
            self.working.preserve = self.working.preserve[-5:]

        # Result summary 업데이트 (standard, full_horcrux)
        if "result_summary" in self._active_layers:
            self.result_summary.content_summary = solution_summary[:300] if solution_summary else ""
            self.result_summary.remaining_blockers = new_blockers[:5]
            self.result_summary.resolved_items.extend(resolved[:3])
            self.result_summary.resolved_items = self.result_summary.resolved_items[-5:]

        # Decision memory 업데이트 (full_horcrux only)
        if "decision" in self._active_layers and score >= 7.0:
            self.decision.accepted_decisions.append({
                "topic": f"round_{round_num}",
                "choice": "approach maintained",
                "reason": f"score {score}/10",
            })
            self.decision.accepted_decisions = self.decision.accepted_decisions[-5:]

        # Checkpoint 생성
        checkpoint = RoundCheckpoint(
            round=round_num,
            score=score,
            current_conclusion=solution_summary[:200] if solution_summary else "",
            what_changed=resolved[:3],
            remaining_blockers=new_blockers[:5],
            preserve=self.working.preserve[:3],
            must_not_change=self.working.must_not_change[:3],
        )
        self.checkpoints.append(checkpoint)

    def inject_human_directive(self, directive: str, action_type: str = "feedback"):
        """Interactive mode: 사람의 피드백/포커스를 memory에 주입."""
        # working memory에 보존 항목으로 추가
        if directive not in self.working.must_not_change:
            self.working.must_not_change.append(f"[HUMAN] {directive}")
        # decision memory에 기록
        if "decision" in self._active_layers:
            self.decision.accepted.append({
                "topic": f"human_{action_type}",
                "choice": directive,
                "reason": f"human directive ({action_type})",
            })

    def build_revision_prompt(
        self,
        task: str,
        round_num: int,
        current_solution_truncated: str,
        human_directive: str = "",
        focus_constraint: str = "",
    ) -> str:
        """delta-based revision 프롬프트 생성.

        Hard rules:
          - full previous output 재삽입 금지
          - critic 전체 목록 재삽입 금지
          - accepted 내용은 preserve로만 전달
          - blocking issue + delta만 전달
        """
        parts = []

        # Human directive (최우선)
        if human_directive:
            parts.append(f"[HUMAN DIRECTIVE — highest priority]\n{human_directive}")

        # Focus constraint
        if focus_constraint:
            parts.append(f"[FOCUS CONSTRAINT]\nFocus on: {focus_constraint}\nDo NOT change areas outside this focus.")

        # Task (항상 포함)
        parts.append(f"Task: {task}")

        # Compact memory blocks (모드별 차등)
        if "working" in self._active_layers:
            block = self.working.to_prompt_block()
            if block:
                parts.append(block)

        if "decision" in self._active_layers:
            block = self.decision.to_prompt_block()
            if block:
                parts.append(block)

        if "result_summary" in self._active_layers:
            block = self.result_summary.to_prompt_block()
            if block:
                parts.append(block)

        # Latest checkpoint (전체 출력 대신)
        if self.checkpoints:
            latest = self.checkpoints[-1]
            parts.append(latest.to_prompt_block())

        # Delta prompt
        prev_blockers = self.checkpoints[-2].remaining_blockers if len(self.checkpoints) >= 2 else []
        curr_blockers = self.working.blocking_issues

        new_blockers = [b for b in curr_blockers if b not in prev_blockers]
        resolved = [b for b in prev_blockers if b not in curr_blockers]

        delta = DeltaPrompt(
            current_state_summary=f"Round {round_num}, score {self.checkpoints[-1].score}/10" if self.checkpoints else "",
            new_blockers=new_blockers if new_blockers else curr_blockers,
            resolved_items=resolved,
            preserve=self.working.preserve[:3],
            next_action="Fix the remaining blockers. Preserve what's working.",
        )
        parts.append(delta.to_prompt_block())

        # 현재 solution은 truncated 버전만 (fast는 더 짧게)
        max_chars = 3000 if self.mode == "fast" else 5000 if self.mode == "standard" else 6000
        if current_solution_truncated:
            truncated = current_solution_truncated[:max_chars]
            parts.append(f"[CURRENT SOLUTION — truncated to {len(truncated)} chars]\n{truncated}")

        parts.append("Provide the improved solution. Focus ONLY on unresolved blockers.")

        return "\n\n".join(parts)

    def build_critic_prompt(self, task: str, solution_truncated: str) -> str:
        """critic용 프롬프트 — checkpoint 기반."""
        parts = [
            "Critically review this solution. Find bugs, issues, improvements.",
            "Score out of 10.",
            "",
            f"Task: {task}",
        ]

        # 이전 checkpoint가 있으면 context로 추가
        if self.checkpoints:
            latest = self.checkpoints[-1]
            parts.append(f"\n{latest.to_prompt_block()}")

        parts.append(f"\nSolution:\n{solution_truncated}")

        return "\n".join(parts)

    def get_last_checkpoint(self) -> Optional[RoundCheckpoint]:
        return self.checkpoints[-1] if self.checkpoints else None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "active_layers": self._active_layers,
            "working": self.working.to_dict(),
            "decision": self.decision.to_dict() if "decision" in self._active_layers else None,
            "result_summary": self.result_summary.to_dict() if "result_summary" in self._active_layers else None,
            "checkpoints": [c.to_dict() for c in self.checkpoints],
        }
