"""
adaptive_orchestrator.py — Adaptive Horcrux Orchestrator

기존 orchestrator.py의 run_debate()를 감싸는 적응형 실행 레이어.

흐름:
  1. classify_task_complexity() → mode 결정
  2. build_stage_plan() → 실행할 stage 구성
  3. mode별 실행 경로 분기:
     - fast: single generator → light critic → optional revision → finalize
     - standard: pair gen → synth → core critic → revision(max 2) → finalize
     - full_horcrux: 기존 run_debate() 활용 + revision hard cap

호환성:
  - 기존 orchestrator.py는 그대로 유지 (backward compatible)
  - adaptive_orchestrator.py는 새 진입점
  - config.json의 adaptive.enabled로 on/off 토글 가능
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# core/adaptive/ 패키지에서 import
from core.adaptive import (
    HorcruxMode, RoutingSource,
    ClassificationResult, StagePlan,
    classify_task_complexity, build_stage_plan,
    should_continue_revision, generate_session_id,
    run_with_timeout_budget, get_config,
    StageStatus, StageResult, TimeoutBudgetResult,
    REVISION_HARD_CAP,
    CompactMemory,
    should_run_aux_critics,
    execute_fallback_chain, FallbackContext, FallbackAction as AdaptiveFallbackAction,
    InteractiveSession, SessionConfig, SessionState,
    FeedbackAction as InteractiveFeedbackAction, SessionCommand, RoundResult,
)
from core.provider import (
    ProviderBackend, ProviderResponse,
    make_core_pair, make_auxiliary, invoke_parallel,
)

CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _truncate(text: str, max_chars: int = 8000) -> str:
    if not text or len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n\n... [TRUNCATED {len(text) - max_chars} chars] ...\n\n" + text[-half:]


def _parse_score(text: str) -> float:
    patterns = [
        r"(?:score|점수|rating)[:\s]*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*/\s*10",
        r"\b(\d+(?:\.\d+)?)\b",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            score = float(m.group(1))
            if 0 <= score <= 10:
                return score
    return 5.0


def _log_round(session_id: str, round_num: int, data: dict):
    log_file = LOG_DIR / f"{session_id}.jsonl"
    entry = {"round": round_num, "timestamp": datetime.now().isoformat(), **data}
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ═══════════════════════════════════════════════════════════
#  Main Entry Point
# ═══════════════════════════════════════════════════════════

def run_adaptive(
    task: str,
    config: Optional[dict] = None,
    mode_override: Optional[str] = None,
    task_type: str = "code",
    num_files: int = 1,
    scope: str = "small",
    risk: str = "low",
    artifact_type: str = "none",
    interactive: str = "batch",
) -> dict:
    """
    Adaptive Horcrux 메인 진입점.

    기존 run_debate()를 대체하는 새 인터페이스.
    난이도에 따라 fast/standard/full_horcrux로 자동 분기.
    """
    if config is None:
        config = load_config()

    adaptive_config = config.get("adaptive", {})
    if not adaptive_config.get("enabled", True):
        # adaptive 비활성화 → 기존 orchestrator 호출
        from orchestrator import run_debate
        return run_debate(task, config)

    session_id = generate_session_id()
    total_start = time.time()
    adaptive_cfg = get_config()

    # ─── Step 1: Task Classification ───
    # 패키지 API는 plain string 인자를 받음
    classification = classify_task_complexity(
        task_description=task,
        task_type=task_type,
        num_files_touched=num_files,
        estimated_scope=scope,
        risk_level=risk,
        artifact_type=artifact_type,
        user_mode_override=mode_override,
    )

    mode = classification.recommended_mode

    # ─── Step 2: Build Stage Plan ───
    stage_plan = build_stage_plan(
        recommended_mode=mode,
        task_type=task_type,
        artifact_type=artifact_type,
    )

    print(f"\n{'='*60}")
    print(f"  Adaptive Horcrux v5.3")
    print(f"{'='*60}")
    print(f"  Task: {task[:100]}...")
    print(f"  Mode: {mode.value} (confidence={classification.confidence:.2f})")
    print(f"  Source: {classification.routing_source.value}")
    print(f"  Reason: {classification.reason}")
    print(f"  Stages: {' → '.join(stage_plan.enabled_stages)}")
    print(f"  Session: {session_id}")
    print(f"{'='*60}\n")

    # ─── Step 2.5: Interactive Session (if not batch) ───
    i_session = None
    if interactive != "batch":
        i_session = InteractiveSession(
            config=SessionConfig(mode=interactive),
            base_dir=str(LOG_DIR / "checkpoints"),
        )

    # ─── Step 3: Mode-specific Execution ───
    if mode == HorcruxMode.FAST:
        result = _run_fast(task, config, session_id, stage_plan)
    elif mode == HorcruxMode.STANDARD:
        result = _run_standard(task, config, session_id, stage_plan, i_session=i_session)
    else:
        result = _run_full_horcrux(task, config, session_id, stage_plan, i_session=i_session)

    # ─── Finalize ───
    total_ms = (time.time() - total_start) * 1000
    result["session_id"] = session_id
    result["mode"] = mode.value
    result["routing_source"] = classification.routing_source.value
    result["routing_confidence"] = classification.confidence
    result["stage_plan"] = stage_plan.to_dict()
    result["total_latency_ms"] = round(total_ms, 2)

    # 결과 저장
    result_file = LOG_DIR / f"{session_id}_result.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"  {'DONE [OK]' if result.get('converged') else 'COMPLETED [!]'}")
    print(f"  Mode: {mode.value} | Rounds: {result.get('rounds', 0)}")
    print(f"  Score: {result.get('final_score', 0)}")
    print(f"  Total: {total_ms:.0f}ms")
    print(f"  Log: {result_file}")
    print(f"{'='*60}\n")

    # 자동 scoring 가중치 튜닝
    try:
        from server import _maybe_auto_tune_scoring
        _maybe_auto_tune_scoring()
    except ImportError:
        pass

    return result


# ═══════════════════════════════════════════════════════════
#  FAST Mode
# ═══════════════════════════════════════════════════════════

def _run_fast(
    task: str, config: dict, session_id: str, plan: StagePlan,
) -> dict:
    """
    Fast mode: single generator → light critic → optional revision → finalize

    - synth 없음, aux critic 없음, convergence loop 없음
    - light_critic은 blocker 여부만 yes/no로 판단
    - blocker 없으면 revision 생략하고 바로 finalize
    """
    threshold = config.get("debate", {}).get("threshold", 8.0)
    adaptive_cfg = get_config()
    core = make_core_pair()
    claude = core["claude"]
    history = []

    # Phase 1.5: Compact Memory (fast = working_memory only)
    memory = CompactMemory(mode="fast", task=task)

    gen_timeout_ms = adaptive_cfg.timeouts.generator_ms

    # ═══ Single Generator ═══
    print(f"  [FAST] Single Generator (Claude)...")
    gen_result = run_with_timeout_budget(
        stage_name="generator",
        tasks=[("claude", lambda: claude.invoke(
            f"You are an expert. Solve this task completely.\n\nTask:\n{task}",
            timeout=gen_timeout_ms // 1000,
        ))],
        timeout_budget_ms=gen_timeout_ms,
        mode=HorcruxMode.FAST.value,
        session_id=session_id,
    )

    gen_text = ""
    for r in gen_result.completed_results:
        if r.status == StageStatus.COMPLETED and r.result:
            gen_text = r.result.text if hasattr(r.result, 'text') else str(r.result)
            break

    if not gen_text:
        return {"converged": False, "rounds": 0, "final_solution": "",
                "final_score": 0, "error": "generator failed"}

    print(f"      → {len(gen_text)} chars")

    # ═══ Light Critic ═══
    print(f"  [FAST] Light Critic (Codex)...")
    codex = core["codex"]
    light_critic_prompt = (
        f"Quickly review this solution. Answer ONLY:\n"
        f"1. BLOCKER: yes or no\n"
        f"2. If yes, describe the critical issue in 1-2 sentences.\n"
        f"3. Score: X/10\n\n"
        f"Task: {task}\n\nSolution:\n{_truncate(gen_text, 6000)}"
    )

    light_timeout_ms = adaptive_cfg.timeouts.light_critic_ms
    critic_result = run_with_timeout_budget(
        stage_name="light_critic",
        tasks=[("codex", lambda: codex.invoke(
            light_critic_prompt,
            timeout=light_timeout_ms // 1000,
        ))],
        timeout_budget_ms=light_timeout_ms,
        mode=HorcruxMode.FAST.value,
        session_id=session_id,
    )

    critic_text = ""
    critic_score = 8.0
    has_blocker = False

    for r in critic_result.completed_results:
        if r.status == StageStatus.COMPLETED and r.result:
            critic_text = r.result.text if hasattr(r.result, 'text') else str(r.result)
            critic_score = _parse_score(critic_text)
            has_blocker = "blocker: yes" in critic_text.lower() or \
                          "blocker:yes" in critic_text.lower()
            break

    print(f"      → Score: {critic_score}/10 | Blocker: {'YES' if has_blocker else 'NO'}")

    round_data = {
        "mode": "fast",
        "generator": gen_text[:500],
        "critic": critic_text[:500],
        "score": critic_score,
        "blocker": has_blocker,
    }

    # Phase 1.5: update memory from critic
    memory.update_from_critic(
        critic_text=critic_text,
        score=critic_score,
        round_num=1,
        solution_summary=gen_text[:200],
    )

    # ═══ Revision (optional, only if blocker) ═══
    final_solution = gen_text
    if has_blocker and critic_score < threshold:
        print(f"  [FAST] Revision (blocker found)...")
        # Phase 1.5: delta-based revision prompt
        revision_prompt = memory.build_revision_prompt(
            task=task,
            round_num=1,
            current_solution_truncated=_truncate(gen_text, 3000),
        )

        rev_timeout_ms = adaptive_cfg.timeouts.revision_ms
        rev_result = run_with_timeout_budget(
            stage_name="revision",
            tasks=[("claude", lambda: claude.invoke(
                revision_prompt,
                timeout=rev_timeout_ms // 1000,
            ))],
            timeout_budget_ms=rev_timeout_ms,
            mode=HorcruxMode.FAST.value,
            session_id=session_id,
        )

        for r in rev_result.completed_results:
            if r.status == StageStatus.COMPLETED and r.result:
                rev_text = r.result.text if hasattr(r.result, 'text') else str(r.result)
                if rev_text:
                    final_solution = rev_text
                    round_data["revised"] = True
                    print(f"      → Revised")
                break
    else:
        print(f"  [FAST] No blocker - skipping revision")

    _log_round(session_id, 1, round_data)
    history.append(round_data)

    return {
        "converged": critic_score >= threshold or not has_blocker,
        "rounds": 1,
        "final_solution": final_solution,
        "final_score": critic_score,
        "history": history,
        "compact_memory": memory.to_dict(),
    }


# ═══════════════════════════════════════════════════════════
#  STANDARD Mode
# ═══════════════════════════════════════════════════════════

def _run_standard(
    task: str, config: dict, session_id: str, plan: StagePlan,
    i_session: Optional[InteractiveSession] = None,
) -> dict:
    """
    Standard mode: pair gen → synth → core critic → revision(max 2) → finalize

    - aux critic 없음
    - revision hard cap 2회
    - convergence는 score threshold 기반
    """
    threshold = config.get("debate", {}).get("threshold", 8.0)
    adaptive_cfg = get_config()
    hard_cap = adaptive_cfg.revision.hard_cap
    core = make_core_pair()
    claude = core["claude"]
    codex = core["codex"]
    history = []
    current_solution = ""
    prev_score = 0.0

    # Phase 1.5: Compact Memory (standard = working + result_summary)
    memory = CompactMemory(mode="standard", task=task)

    for round_num in range(1, hard_cap + 1):
        print(f"\n  [STD] Round {round_num}/{hard_cap}")
        round_data = {"round": round_num, "mode": "standard"}

        # ═══ Pair Generation ═══
        if round_num == 1:
            gen_prompt = (
                f"You are an expert. Solve this task thoroughly.\n"
                f"Provide complete, production-ready output.\n\nTask:\n{task}"
            )
        else:
            # Phase 1.5: delta-based prompting (전체 재삽입 금지)
            hd = i_session.get_pending_directive() if i_session else None
            fc = i_session.get_pending_focus() if i_session else None
            gen_prompt = memory.build_revision_prompt(
                task=task,
                round_num=round_num,
                current_solution_truncated=_truncate(current_solution, 5000),
                human_directive=hd or "",
                focus_constraint=fc or "",
            )

        pair_timeout_ms = adaptive_cfg.timeouts.generator_ms
        print(f"    [1] Pair Generation...")
        gen_budget = run_with_timeout_budget(
            stage_name="pair_generation",
            tasks=[
                ("claude", lambda p=gen_prompt: claude.invoke(p, timeout=pair_timeout_ms // 1000)),
                ("codex", lambda p=gen_prompt: codex.invoke(p, timeout=pair_timeout_ms // 1000)),
            ],
            timeout_budget_ms=pair_timeout_ms,
            mode=HorcruxMode.STANDARD.value,
            session_id=session_id,
        )

        solutions = []
        for r in gen_budget.completed_results:
            if r.status == StageStatus.COMPLETED and r.result:
                text = r.result.text if hasattr(r.result, 'text') else str(r.result)
                if text:
                    solutions.append((r.task_id, text))

        if not solutions:
            round_data["error"] = "all generators failed"
            history.append(round_data)
            _log_round(session_id, round_num, round_data)
            break

        print(f"        → {len(solutions)} solutions")

        # ═══ Synth (best candidate) ═══
        if len(solutions) > 1:
            best = max(solutions, key=lambda x: len(x[1]))
            current_solution = best[1]
            round_data["synth"] = f"selected {best[0]} ({len(best[1])} chars)"
        else:
            current_solution = solutions[0][1]
            round_data["synth"] = f"single result from {solutions[0][0]}"

        print(f"    [2] Synth: {round_data['synth']}")

        # ═══ Core Critic ═══
        # Phase 1.5: checkpoint 기반 critic prompt
        critic_prompt = memory.build_critic_prompt(
            task=task,
            solution_truncated=_truncate(current_solution, 8000),
        )

        critic_model = codex if solutions[0][0] == "claude" else claude
        critic_name = "codex" if solutions[0][0] == "claude" else "claude"
        critic_timeout_ms = adaptive_cfg.timeouts.core_critic_ms

        print(f"    [3] Core Critic ({critic_name})...")
        critic_budget = run_with_timeout_budget(
            stage_name="core_critic",
            tasks=[(critic_name, lambda: critic_model.invoke(
                critic_prompt,
                timeout=critic_timeout_ms // 1000,
            ))],
            timeout_budget_ms=critic_timeout_ms,
            mode=HorcruxMode.STANDARD.value,
            session_id=session_id,
            retry_on_failure=True,
        )

        critic_text = ""
        critic_score = 5.0
        for r in critic_budget.completed_results:
            if r.status == StageStatus.COMPLETED and r.result:
                critic_text = r.result.text if hasattr(r.result, 'text') else str(r.result)
                critic_score = _parse_score(critic_text)
                break

        if critic_budget.unresolved_flag:
            critic_text = "[UNRESOLVED] critic timed out after retry"
            critic_score = prev_score if prev_score > 0 else 5.0

        # Phase 2: conditional aux critics
        aux_decision = should_run_aux_critics(
            mode=HorcruxMode.STANDARD.value,
            core_scores={critic_name: critic_score},
            critical_issue_count=len(memory.working.blocking_issues),
            risk_level=config.get("adaptive", {}).get("risk", "medium"),
        )
        if aux_decision.run_aux:
            print(f"    [3b] Aux Critics (reason: {aux_decision.reason})...")
            try:
                aux_providers = make_auxiliary()
                aux_scores = []
                for name, provider in aux_providers.items():
                    try:
                        aux_resp = provider.invoke(critic_prompt, timeout=60)
                        if aux_resp.ok:
                            aux_score = _parse_score(aux_resp.text)
                            aux_scores.append(aux_score)
                            print(f"        → {name}: {aux_score}/10")
                    except Exception:
                        pass
                if aux_scores:
                    # Weighted: Core × 0.8 + Aux avg × 0.2
                    aux_avg = sum(aux_scores) / len(aux_scores)
                    critic_score = critic_score * 0.8 + aux_avg * 0.2
                    print(f"        → Combined: {critic_score:.1f}/10")
            except Exception:
                pass
        else:
            print(f"    [3b] Aux skipped: {aux_decision.reason}")

        # Phase 1.5: compact memory 업데이트 + checkpoint 생성
        memory.update_from_critic(
            critic_text=critic_text,
            score=critic_score,
            round_num=round_num,
            solution_summary=current_solution[:300],
        )

        round_data["critic"] = critic_text[:1000]
        round_data["score"] = critic_score
        round_data["compact_memory"] = memory.to_dict()
        print(f"        → {critic_score}/10 (checkpoint R{round_num} saved)")

        # ═══ Convergence Check ═══
        if critic_score >= threshold:
            print(f"    [CONVERGED] ({critic_score} >= {threshold})")
            round_data["converged"] = True
            history.append(round_data)
            _log_round(session_id, round_num, round_data)
            if i_session:
                i_session.current_round = round_num
                i_session.rounds.append(RoundResult(
                    round_num=round_num, final_score=critic_score,
                    convergence_delta=critic_score - prev_score if round_num > 1 else critic_score,
                ))
                i_session.create_checkpoint()
            break

        # ═══ Interactive Pause Point ═══
        if i_session:
            i_session.current_round = round_num
            rr = RoundResult(
                round_num=round_num, final_score=critic_score,
                convergence_delta=critic_score - prev_score if round_num > 1 else critic_score,
            )
            i_session.rounds.append(rr)
            i_session.create_checkpoint()

            # semi_interactive: auto-pause 조건 체크
            auto_reason = i_session.should_auto_pause(rr)
            if auto_reason:
                i_session.pause(reason=auto_reason)

            if not i_session.check_pause_point(f"after_critic_round_{round_num}"):
                print(f"    [CANCELLED] Session cancelled")
                break

            # resume 후 human directive 반영
            hd = i_session.get_pending_directive()
            if hd:
                print(f"    [HUMAN] Directive: {hd[:80]}")

        # ═══ Revision Decision ═══
        progress_delta = critic_score - prev_score if round_num > 1 else critic_score
        prev_checkpoint_blockers = memory.checkpoints[-2].remaining_blockers if len(memory.checkpoints) >= 2 else []
        curr_blockers = memory.working.blocking_issues

        revision_decision = should_continue_revision(
            current_round=round_num,
            converged=False,
            blocking_issue_count=len(curr_blockers),
            progress_delta=progress_delta,
            timeout_budget_remaining_ms=adaptive_cfg.timeouts.revision_ms,
            previous_blockers=prev_checkpoint_blockers,
            current_blockers=curr_blockers,
        )

        round_data["converged"] = False
        round_data["revision_decision"] = revision_decision.reason
        history.append(round_data)
        _log_round(session_id, round_num, round_data)

        if not revision_decision.should_continue:
            print(f"    [STOP] Stopping: {revision_decision.reason}")
            break

        prev_score = critic_score
        print(f"    → Continuing to round {round_num + 1}")

    final_score = history[-1].get("score", 0) if history else 0
    converged = history[-1].get("converged", False) if history else False

    return {
        "converged": converged,
        "rounds": len(history),
        "final_solution": current_solution,
        "final_score": final_score,
        "history": history,
        "compact_memory": memory.to_dict(),
    }


# ═══════════════════════════════════════════════════════════
#  FULL HORCRUX Mode
# ═══════════════════════════════════════════════════════════

def _run_full_horcrux(
    task: str, config: dict, session_id: str, plan: StagePlan,
    i_session: Optional[InteractiveSession] = None,
) -> dict:
    """
    Full Horcrux mode: 기존 orchestrator.run_debate() 활용.
    revision hard cap만 추가 적용.
    """
    adaptive_cfg = get_config()
    hard_cap = adaptive_cfg.revision.hard_cap

    modified_config = json.loads(json.dumps(config))
    modified_config["debate"]["max_rounds"] = hard_cap

    print(f"  [FULL] Delegating to full debate loop (max {hard_cap} rounds)...")

    from orchestrator import run_debate
    result = run_debate(task, modified_config)

    return {
        "converged": result.get("converged", False),
        "rounds": result.get("rounds", 0),
        "final_solution": result.get("final_solution", ""),
        "final_score": result.get("final_score", 0),
        "history": result.get("history", []),
    }


# ═══════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Horcrux v8 - Adaptive Multi-AI Orchestration CLI",
        epilog="""Examples:
  python adaptive_orchestrator.py "fix typo in README"
  python adaptive_orchestrator.py --mode fast "simple bug fix"
  python adaptive_orchestrator.py --mode full --risk high -f task.txt
  python adaptive_orchestrator.py --mode parallel "build frontend + backend"
  python adaptive_orchestrator.py classify "refactor architecture"
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("task", nargs="?", help="Task description (or 'classify' to preview routing)")
    parser.add_argument("task_extra", nargs="?", help="Task when first arg is 'classify'")
    parser.add_argument("--file", "-f", help="Read task from file")
    parser.add_argument("--mode", "-m", default="auto",
                        choices=["auto", "fast", "standard", "full", "parallel"],
                        help="Mode (default: auto)")
    parser.add_argument("--type", "-t", default="code",
                        choices=["code", "document", "artifact", "analysis"],
                        help="Task type")
    parser.add_argument("--files", type=int, default=1, help="Number of files touched")
    parser.add_argument("--scope", default="medium", choices=["small", "medium", "large"])
    parser.add_argument("--risk", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--artifact", default="none", choices=["none", "ppt", "pdf", "doc"])
    parser.add_argument("--iterations", type=int, default=3, help="Self-improve iterations")
    parser.add_argument("--pair-mode", default="pair2", choices=["pair2", "pair3"],
                        help="Parallel mode: pair2 or pair3")
    parser.add_argument("--server", action="store_true",
                        help="Use running Flask server instead of direct orchestrator")

    args = parser.parse_args()

    # classify 서브커맨드
    is_classify = args.task == "classify"
    if is_classify:
        task = args.task_extra or ""
    elif args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            task = f.read().strip()
    elif args.task:
        task = args.task
    else:
        parser.print_help()
        sys.exit(1)

    if not task.strip():
        print("Error: task is required")
        sys.exit(1)

    # --server: Flask API 경유
    if args.server:
        import requests
        base = "http://localhost:5000"
        if is_classify:
            r = requests.post(f"{base}/api/horcrux/classify", json={
                "task": task, "scope": args.scope, "risk": args.risk,
                "artifact_type": args.artifact,
            }, timeout=30).json()
            print(f"\nMode:   {r.get('recommended_mode')}")
            print(f"Engine: {r.get('internal_engine')}")
            print(f"Intent: {r.get('detected_intent')}")
            print(f"Conf:   {r.get('confidence', 0):.0%}")
            print(f"Reason: {r.get('reason')}")
            return

        r = requests.post(f"{base}/api/horcrux/run", json={
            "task": task, "mode": args.mode, "scope": args.scope,
            "risk": args.risk, "artifact_type": args.artifact,
            "pair_mode": args.pair_mode, "iterations": args.iterations,
        }, timeout=600).json()

        if r.get("error"):
            print(f"Error: {r['error']}")
            sys.exit(1)
        if r.get("solution"):
            print(f"\n[{r.get('mode')} / {r.get('internal_engine')}] Score: {r.get('score', 0)}/10\n")
            print(r["solution"])
        elif r.get("job_id"):
            print(f"Job started: {r['job_id']} ({r.get('internal_engine')})")
            print(f"Poll: curl {base}/api/{_status_path(r['job_id'])}")
        return

    # classify only (direct)
    if is_classify:
        from core.adaptive import classify_task_complexity
        mode_override = None if args.mode == "auto" else args.mode
        result = classify_task_complexity(
            task_description=task,
            task_type=args.type,
            num_files_touched=args.files,
            estimated_scope=args.scope,
            risk_level=args.risk,
            artifact_type=args.artifact,
            user_mode_override=mode_override,
        )
        d = result.to_dict()
        print(f"\nMode:   {d['recommended_mode']}")
        print(f"Engine: {d['internal_engine']}")
        print(f"Intent: {d['detected_intent']}")
        print(f"Conf:   {d['confidence']:.0%}")
        print(f"Source: {d['routing_source']}")
        print(f"Reason: {d['reason']}")
        return

    # direct execution
    mode_override = None if args.mode == "auto" else args.mode
    # full → full_horcrux (orchestrator 내부 호환)
    if mode_override == "full":
        mode_override = "full_horcrux"

    result = run_adaptive(
        task=task,
        mode_override=mode_override,
        task_type=args.type,
        num_files=args.files,
        scope=args.scope,
        risk=args.risk,
        artifact_type=args.artifact,
    )

    mode = result.get("mode", "?")
    score = result.get("final_score", 0)
    converged = result.get("converged", False)
    print(f"\n[{mode}] {'CONVERGED' if converged else 'COMPLETED'} - Score: {score}/10\n")

    if result.get("final_solution"):
        print(result["final_solution"])


def _status_path(job_id):
    """job_id prefix → status endpoint path"""
    if job_id.startswith("plan_"): return f"planning/status/{job_id}"
    if job_id.startswith("pair_"): return f"pair/status/{job_id}"
    if job_id.startswith("si_"): return f"self_improve/status/{job_id}"
    return f"status/{job_id}"


if __name__ == "__main__":
    main()
