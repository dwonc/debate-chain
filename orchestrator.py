"""
Horcrux Orchestrator v5 — 2-Pair Core + Open Source Hybrid

Architecture:
  Core Pair:  Claude Opus 4.6 (CLI) ↔ Codex 5.4 (CLI)
  Auxiliary:  Groq / Together / OpenRouter (무료 API)

Debate Flow:
  Round N:
    1. Generator (Claude or Codex, alternating) → 솔루션 생성
    2. Critic (상대 Core) + Aux Critics (오픈소스, 병렬) → 비판
    3. Judge (Core 중 하나) → 점수 + 수렴 판정
    4. Synthesizer (Generator) → 피드백 반영 개선
    → 수렴 or 다음 라운드

Key Changes from v4:
  - Gemini 의존성 제거
  - 2-pair 교차 debate (A generates → B criticizes → swap)
  - 오픈소스 API 병렬 호출로 추가 관점 확보
  - 비용 추적: Core(유료) vs Aux(무료) 분리
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.provider import (
    ProviderBackend, ProviderResponse,
    ClaudeCLIBackend, CodexCLIBackend, GeminiCLIBackend, OpenSourceAPIBackend,
    invoke_parallel, make_core_pair, make_core_trio, make_auxiliary,
)


CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def log_round(session_id: str, round_num: int, data: dict):
    log_file = LOG_DIR / f"{session_id}.jsonl"
    entry = {"round": round_num, "timestamp": datetime.now().isoformat(), **data}
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ─── 유틸 ───

def _get_timeout(round_num: int, config: dict) -> int:
    timeouts = config.get("timeouts", {})
    key = f"round_{round_num}"
    return timeouts.get(key, timeouts.get("default", 300))


def _truncate(text: str, max_chars: int = 8000) -> str:
    if not text or len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n... [TRUNCATED {len(text) - max_chars} chars] ...\n\n"
        + text[-half:]
    )


def _summarize_history(history: list, keep_last: int = 2) -> str:
    if len(history) <= keep_last:
        return ""
    old = history[:-keep_last]
    lines = []
    for i, rd in enumerate(old, 1):
        cs = rd.get("core_critic_score", "?")
        js = rd.get("judge_score", "?")
        lines.append(f"  Round {i}: critic={cs}/10, judge={js}/10")
    return "Previous rounds:\n" + "\n".join(lines)


def parse_score(text: str) -> float:
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


# ─── 2-Pair Debate Loop ───

def run_debate(task: str, config: dict = None) -> dict:
    """
    v5 메인 디베이트 루프.

    Core Pair가 교대로 Generator/Critic 역할을 수행하고,
    오픈소스 Auxiliary가 병렬로 추가 비판을 제공.
    """
    if config is None:
        config = load_config()

    threshold = config.get("debate", {}).get("threshold", 8.0)
    max_rounds = config.get("debate", {}).get("max_rounds", 10)
    parallel_critics = config.get("debate", {}).get("parallel_critics", True)
    aux_as_tiebreaker = config.get("debate", {}).get("aux_as_tiebreaker", True)
    core_weight = config.get("scoring", {}).get("core_weight", 0.8)
    aux_weight = config.get("scoring", {}).get("aux_weight", 0.2)
    ctx_config = config.get("context", {})
    max_prompt = ctx_config.get("max_prompt_chars", 16000)

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Provider 초기화
    print(f"\n{'='*60}")
    print(f"  Horcrux v6 — Core Trio (Claude+Codex+Gemini) + Aux Hybrid")
    print(f"{'='*60}")
    print(f"  Task: {task[:100]}...")
    print(f"  Threshold: {threshold} | Max Rounds: {max_rounds}")
    print(f"  Session: {session_id}")
    print(f"\n  [Core Trio]")

    core = make_core_trio()
    claude_gen: ProviderBackend = core["claude_gen"]    # Opus — 생성/개선
    claude_crit: ProviderBackend = core["claude_crit"]  # Sonnet — 비평/심판
    codex: ProviderBackend = core["codex"]
    gemini: ProviderBackend = core["gemini"]
    print(f"    A: Claude Opus 4.6 (CLI) — Generator/Synthesizer")
    print(f"    A': Claude Sonnet 4.6 (CLI) — Critic/Judge (비용 최적화)")
    print(f"    B: Codex 5.4 (CLI) — Generator/Critic/Judge")
    print(f"    C: Gemini 3.1 Pro (CLI) — Generator/Critic/Judge")

    # 3-pair 라운드 로빈 스케줄
    # Generator는 Opus/full 모델, Critic/Judge는 Sonnet/경량 모델
    _trio_schedule = [
        # (generator, [critic1, critic2], judge, names)
        (claude_gen, [codex, gemini], gemini, ("Claude(Opus)", "Codex", "Gemini")),
        (codex, [claude_crit, gemini], claude_crit, ("Codex", "Claude(Sonnet)", "Gemini")),
        (gemini, [claude_crit, codex], codex, ("Gemini", "Claude(Sonnet)", "Codex")),
    ]

    print(f"\n  [Auxiliary — Free API]")
    aux_backends = make_auxiliary(config)
    if not aux_backends:
        print(f"    (none available — core-only mode)")

    print(f"{'='*60}\n")

    # ─── Debate State ───
    current_solution = ""
    history: List[dict] = []

    for round_num in range(1, max_rounds + 1):
        print(f"\n{'─'*50}")
        print(f"  Round {round_num}/{max_rounds}")
        print(f"{'─'*50}")

        timeout = _get_timeout(round_num, config)
        aux_timeout = config.get("timeouts", {}).get("aux_api", 60)
        round_data = {"task": task}

        # ═══ Step 1: Generator ═══
        # 3-pair 라운드 로빈: Claude → Codex → Gemini → Claude → ...
        schedule_idx = (round_num - 1) % len(_trio_schedule)
        generator, core_critics, judge_model, names = _trio_schedule[schedule_idx]
        gen_name = names[0]
        crit_names = [names[1], names[2]]
        # 첫 번째 critic을 메인 critic으로 사용 (점수 계산 기준)
        critic_core = core_critics[0]
        crit_name = crit_names[0]

        if round_num == 1:
            gen_prompt = (
                f"You are an expert developer. Solve this task thoroughly.\n"
                f"Provide complete, production-ready output with explanations.\n"
                f"IMPORTANT: NEVER truncate or cut off your response. If the output is long, complete it fully.\n"
                f"Do NOT use '...' or '[truncated]' or skip sections. Write everything to completion.\n\n"
                f"Task:\n{task}"
            )
        else:
            old_summary = _summarize_history(history)
            prev = history[-1]
            prev_critic = _truncate(prev.get("core_critic", ""), 4000)
            prev_aux = _truncate(prev.get("aux_critics_merged", ""), 2000)
            prev_judge = _truncate(prev.get("judge_feedback", ""), 2000)
            prev_solution = _truncate(current_solution, max_prompt // 2)

            gen_prompt = (
                f"{old_summary}\n\n" if old_summary else ""
            ) + (
                f"Previous solution:\n{prev_solution}\n\n"
                f"Core Critic ({crit_name}):\n{prev_critic}\n\n"
            )
            if prev_aux:
                gen_prompt += f"Auxiliary Critics:\n{prev_aux}\n\n"
            if prev_judge:
                gen_prompt += f"Judge Feedback:\n{prev_judge}\n\n"
            gen_prompt += (
                f"Improve the solution addressing ALL feedback.\n"
                f"Task:\n{task}"
            )

        print(f"  [1] Generator ({gen_name})... [{len(gen_prompt)} chars]")
        gen_response = generator.invoke(gen_prompt, timeout)
        round_data["generator"] = gen_name
        round_data["gen_response"] = gen_response.text
        round_data["gen_latency_ms"] = gen_response.latency_ms
        print(f"      → {len(gen_response.text)} chars ({gen_response.latency_ms}ms)")

        if not gen_response.ok:
            print(f"      ⚠️ Generator error: {gen_response.error}")
            round_data["error"] = gen_response.error
            log_round(session_id, round_num, round_data)
            history.append(round_data)
            continue

        # ═══ Step 2: Critics (Core + Aux 병렬) ═══
        solution_for_review = _truncate(gen_response.text, max_prompt)

        critic_prompt = (
            f"You are a senior expert reviewer. Critically analyze this solution.\n"
            f"Find: bugs, edge cases, performance issues, security flaws, missing features.\n"
            f"Be specific and actionable. Give a score out of 10.\n\n"
            f"Task: {task}\n\n"
            f"Solution by {gen_name}:\n{solution_for_review}"
        )

        # Core Critics (2개 모델 병렬) + Auxiliary Critics
        print(f"  [2] Core Critics ({crit_names[0]}+{crit_names[1]})...", end="", flush=True)

        aux_responses: List[ProviderResponse] = []
        # Core 2개 + Aux 모두 병렬 실행
        all_critics = core_critics[:]  # [critic1, critic2]
        if parallel_critics and aux_backends:
            print(f" + {len(aux_backends)} aux...", end="", flush=True)
            all_critics += aux_backends
        all_responses = invoke_parallel(all_critics, critic_prompt, timeout)

        # Core critic 1 (메인)
        core_critic_resp = all_responses[0]
        core_critic_score = parse_score(core_critic_resp.text)
        round_data["core_critic"] = core_critic_resp.text
        round_data["core_critic_score"] = core_critic_score
        round_data["core_critic_latency_ms"] = core_critic_resp.latency_ms
        print(f"\n      → Core[{crit_names[0]}]: {core_critic_score}/10 ({core_critic_resp.latency_ms}ms)")

        # Core critic 2 (세컨드)
        core_critic2_resp = all_responses[1]
        core_critic2_score = parse_score(core_critic2_resp.text)
        round_data["core_critic2"] = core_critic2_resp.text
        round_data["core_critic2_score"] = core_critic2_score
        print(f"      → Core[{crit_names[1]}]: {core_critic2_score}/10 ({core_critic2_resp.latency_ms}ms)")

        # Aux responses (index 2부터)
        aux_responses = all_responses[2:]

        # Aux scores
        aux_scores = []
        aux_texts = []
        for i, ar in enumerate(aux_responses):
            if ar.ok:
                s = parse_score(ar.text)
                aux_scores.append(s)
                aux_texts.append(f"[{ar.provider}/{ar.model}] score={s}/10\n{ar.text[:500]}")
                print(f"      → Aux[{ar.provider}]: {s}/10 ({ar.latency_ms}ms)")
            else:
                print(f"      → Aux[{ar.provider}]: FAILED ({ar.error})")

        round_data["aux_critics"] = aux_texts
        round_data["aux_scores"] = aux_scores
        round_data["aux_critics_merged"] = "\n---\n".join(aux_texts) if aux_texts else ""

        # ═══ Step 3: 점수 계산 (Core Trio 가중 평균) ═══
        # Core1 40% + Core2 40% + Aux avg 20%
        core_avg = (core_critic_score + core_critic2_score) / 2
        if aux_scores and aux_as_tiebreaker:
            aux_avg = sum(aux_scores) / len(aux_scores)
            weighted_score = (core_avg * 0.8) + (aux_avg * 0.2)
        else:
            weighted_score = core_avg

        round_data["weighted_score"] = round(weighted_score, 2)
        print(f"      → Weighted Score: {weighted_score:.2f}/10")

        # ═══ Step 4: 수렴 확인 ═══
        if weighted_score >= threshold:
            print(f"\n  ✅ CONVERGED! Score {weighted_score:.2f} >= {threshold}")
            current_solution = gen_response.text
            round_data["converged"] = True
            log_round(session_id, round_num, round_data)
            history.append(round_data)
            break

        # ═══ Step 5: Judge (Generator도 Critic도 아닌 제3 모델이 판정) ═══
        judge_name = names[2]  # 라운드 로빈에서 judge 역할 모델명
        judge_prompt = (
            f"As an impartial judge, evaluate the solution quality.\n"
            f"You did NOT generate or critique this solution — you are a neutral third party.\n"
            f"1. List the top 3 most impactful issues found by critics.\n"
            f"2. Acknowledge what the solution does well.\n"
            f"3. Give your final verdict: Score: X/10 (be fair — 5=average, 7=good, 9=excellent)\n\n"
            f"Solution by {gen_name}:\n{_truncate(gen_response.text, 6000)}\n\n"
            f"Critic [{crit_names[0]}] ({core_critic_score}/10):\n{_truncate(core_critic_resp.text, 3000)}\n\n"
            f"Critic [{crit_names[1]}] ({core_critic2_score}/10):\n{_truncate(core_critic2_resp.text, 3000)}\n\n"
        )
        if aux_texts:
            judge_prompt += f"Auxiliary Critics:\n{_truncate(round_data['aux_critics_merged'], 2000)}\n\n"

        print(f"  [3] Judge ({judge_name})...")
        judge_resp = judge_model.invoke(judge_prompt, timeout)
        judge_score = parse_score(judge_resp.text)
        round_data["judge_feedback"] = judge_resp.text
        round_data["judge_score"] = judge_score
        print(f"      → Judge Score: {judge_score}/10")

        # ═══ Step 6: Synthesizer (Generator가 개선) ═══
        synth_prompt = (
            f"You generated the previous solution. Now improve it based on ALL feedback.\n"
            f"Focus on the judge's top priorities.\n\n"
            f"Original task: {task}\n\n"
            f"Your solution:\n{_truncate(gen_response.text, max_prompt // 2)}\n\n"
            f"Judge ({judge_name})'s priorities:\n{_truncate(judge_resp.text, 4000)}\n\n"
            f"Critic [{crit_names[0]}]:\n{_truncate(core_critic_resp.text, 2500)}\n\n"
            f"Critic [{crit_names[1]}]:\n{_truncate(core_critic2_resp.text, 2500)}\n\n"
            f"Create an improved, complete solution.\n"
            f"IMPORTANT: NEVER truncate your output. Complete every section, table, and chart fully."
        )

        print(f"  [4] Synthesizer ({gen_name})...")
        synth_resp = generator.invoke(synth_prompt, timeout)
        current_solution = synth_resp.text if synth_resp.ok else gen_response.text
        round_data["synthesizer"] = synth_resp.text
        round_data["synth_latency_ms"] = synth_resp.latency_ms
        round_data["converged"] = False
        print(f"      → {len(current_solution)} chars ({synth_resp.latency_ms}ms)")

        log_round(session_id, round_num, round_data)
        history.append(round_data)

    # ─── 최종 결과 ───
    final_score = history[-1].get("weighted_score", 0) if history else 0
    converged = history[-1].get("converged", False) if history else False

    # 비용 요약
    total_core_calls = sum(
        2 + (1 if not h.get("converged") else 0) * 2  # gen + critic + judge + synth
        for h in history
    )
    total_aux_calls = sum(len(h.get("aux_scores", [])) for h in history)

    result = {
        "session_id": session_id,
        "version": "5.0",
        "task": task,
        "rounds": len(history),
        "final_score": final_score,
        "converged": converged,
        "final_solution": current_solution,
        "stats": {
            "core_cli_calls": total_core_calls,
            "aux_api_calls": total_aux_calls,
            "aux_cost_usd": 0.0,
        },
        "history": history,
    }

    result_file = LOG_DIR / f"{session_id}_result.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"  {'CONVERGED ✅' if converged else 'MAX ROUNDS ⚠️'}")
    print(f"  Rounds: {result['rounds']} | Score: {final_score}")
    print(f"  Core CLI calls: {total_core_calls} | Aux API calls: {total_aux_calls}")
    print(f"  Log: {result_file}")
    print(f"{'='*60}\n")

    return result


# ─── CLI ───

def main():
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py <task>")
        print("  or:  python orchestrator.py --file <task_file.txt>")
        sys.exit(1)

    if sys.argv[1] == "--file":
        with open(sys.argv[2], "r", encoding="utf-8") as f:
            task = f.read().strip()
    else:
        task = " ".join(sys.argv[1:])

    result = run_debate(task)

    if result["converged"]:
        print("\n📋 Final Solution:\n")
        print(result["final_solution"])


if __name__ == "__main__":
    main()
