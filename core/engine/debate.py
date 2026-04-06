"""
core/engine/debate.py — R18: Debate engine extracted from server.py

run_multi_critic: 5+ model parallel critic
run_debate: Generator → MultiCritic → Synthesizer → Convergence loop
"""

import json
import os
import time
import concurrent.futures
from datetime import datetime
from pathlib import Path

from core.llm import (
    call_claude, call_codex, call_gemini, call_gemini_fast,
    _call_aux_critic, AUX_CRITIC_ENDPOINTS, AUX_MAX_PROMPT_CHARS,
    _truncate_for_aux, MAX_PROMPT_CHARS,
)
from core.prompts import (
    GENERATOR_PROMPT, GENERATOR_IMPROVE_PROMPT, GENERATOR_IMPROVE_PROMPT_V2,
    CRITIC_PROMPT, SYNTHESIZER_PROMPT,
)
from core.engine.critic import (
    extract_json, extract_score, normalize_critic_output,
    check_convergence_v2, build_revision_focus,
    build_compact_context_package, format_issues_compact,
)

LOG_DIR = Path(__file__).parent.parent.parent / "logs"

# R15: scoring weights cache
_scoring_cache = {}
_scoring_cache_ts = 0


def _get_scoring_weights() -> tuple:
    """config.json의 scoring weights를 캐시 (60초 TTL)."""
    global _scoring_cache, _scoring_cache_ts
    now = time.time()
    if now - _scoring_cache_ts < 60 and _scoring_cache:
        return _scoring_cache.get("core_weight", 0.8), _scoring_cache.get("aux_weight", 0.2)
    try:
        config_path = Path(__file__).parent.parent.parent / "config.json"
        with open(config_path, "r", encoding="utf-8") as f:
            _scoring_cache = json.load(f).get("scoring", {})
            _scoring_cache_ts = now
    except Exception:
        pass
    return _scoring_cache.get("core_weight", 0.8), _scoring_cache.get("aux_weight", 0.2)


def run_multi_critic(task, solution, previously_fixed_text, vision_url="", vision_mode="full"):
    """Phase 2+: Codex + Gemini + Aux(Groq/Together/OpenRouter) + Vision 병렬 Critic"""
    prompt = CRITIC_PROMPT.format(
        task=task, solution=solution,
        previously_fixed=previously_fixed_text or "None (first round)"
    )

    # 사용 가능한 Aux 엔드포인트 수집
    available_aux = [
        ep for ep in AUX_CRITIC_ENDPOINTS
        if os.environ.get(ep[2])
    ]
    # Vision critic 활성화 판정: full 모드 + vision_url 존재 시
    run_vision = bool(vision_url and vision_mode == "full_horcrux")

    total_workers = 2 + len(available_aux) + (1 if run_vision else 0)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(total_workers, 2)) as pool:
        # Core critics
        f_codex = pool.submit(call_codex, prompt)
        f_gemini = pool.submit(call_gemini, prompt)

        # Aux critics (병렬)
        aux_futures = []
        for name, base, env_key, model, extra_h in available_aux:
            f = pool.submit(_call_aux_critic, name, base, env_key, model, extra_h, prompt)
            aux_futures.append(f)

        # Vision UI critic (병렬, full 모드 전용)
        f_vision = None
        if run_vision:
            from core.vision.critic import run_vision_critic
            f_vision = pool.submit(run_vision_critic, vision_url, "desktop", "light")

        # R21/P0-001: timeout 적용 — 무한 블로킹 제거
        # P1-005: degraded 추적
        _CRITIC_TIMEOUT = 180  # seconds
        _degraded_critics = []
        try:
            codex_raw = f_codex.result(timeout=_CRITIC_TIMEOUT)
        except Exception as e:
            codex_raw = f"[ERROR] Codex critic timeout/error: {e}"
            _degraded_critics.append("Codex")
        try:
            gemini_raw = f_gemini.result(timeout=_CRITIC_TIMEOUT)
        except Exception as e:
            gemini_raw = f"[ERROR] Gemini critic timeout/error: {e}"
            _degraded_critics.append("Gemini")
        aux_results = []
        for f in aux_futures:
            try:
                aux_results.append(f.result(timeout=_CRITIC_TIMEOUT))
            except Exception as e:
                aux_results.append(("aux_timeout", f"[ERROR] Aux critic timeout: {e}"))
                _degraded_critics.append("aux")
        try:
            vision_result = f_vision.result(timeout=_CRITIC_TIMEOUT) if f_vision else None
        except Exception as e:
            vision_result = None
            _degraded_critics.append("vision")

    codex_data = extract_json(codex_raw) or {}
    gemini_data = extract_json(gemini_raw) or {}

    # v5.2+: normalize_critic_output으로 공통 schema 변환
    codex_norm = normalize_critic_output(codex_data, "Codex")
    gemini_norm = normalize_critic_output(gemini_data, "Gemini")

    codex_score = codex_norm["score"]
    gemini_score = gemini_norm["score"]

    # Aux 점수 수집 (normalized)
    aux_scores = {}
    aux_norms = []
    for name, raw in aux_results:
        if not raw:
            continue
        data = extract_json(raw) or {}
        norm = normalize_critic_output(data, name)
        aux_scores[name] = norm["score"]
        aux_norms.append(norm)

    # R15: scoring config 캐시 (매번 디스크 읽기 제거)
    _core_w, _aux_w = _get_scoring_weights()

    core_min = min(codex_score, gemini_score)
    if aux_scores:
        aux_avg = sum(aux_scores.values()) / len(aux_scores)
        overall = core_min * _core_w + aux_avg * _aux_w
    else:
        overall = core_min

    # 차원별 최소값 (core만, aux는 참고)
    merged_dims = {}
    for dim in ["correctness", "completeness", "security", "performance"]:
        vals = []
        for norm in [codex_norm, gemini_norm]:
            v = norm.get("dimension_scores", {}).get(dim)
            if v is not None:
                vals.append(float(v))
        merged_dims[dim] = min(vals) if vals else 5.0

    # 이슈 합산 + 중복 제거 (Core + Aux, normalized issues 사용)
    all_issues = []
    seen = set()
    all_norms = [codex_norm, gemini_norm] + aux_norms
    for norm in all_norms:
        for iss in norm.get("issues", []):
            key = iss.get("summary", iss.get("desc", ""))[:40]
            if key and key not in seen:
                seen.add(key)
                all_issues.append(iss)

    # regression 합산 (normalized)
    all_regressions = []
    for norm in all_norms:
        all_regressions.extend(norm.get("regressions", []))
    # 문자열 regression 중복 제거
    reg_seen = set()
    regressions = []
    for r in all_regressions:
        key = r.get("summary", str(r))[:60] if isinstance(r, dict) else str(r)[:60]
        if key and key not in reg_seen:
            reg_seen.add(key)
            regressions.append(r)

    # critic_scores 합산
    critic_scores = {"Codex": codex_score, "Gemini": gemini_score}
    critic_scores.update(aux_scores)

    # ── Vision UI Critic 결과 합류 ──
    vision_data = {}
    if vision_result and vision_result.get("ok"):
        vision_score = vision_result.get("score", 0.0)
        critic_scores["Vision"] = vision_score
        # vision issues → all_issues에 추가
        for vi in vision_result.get("issues", []):
            desc = vi.get("description", "")
            key = desc[:40]
            if key and key not in seen:
                seen.add(key)
                all_issues.append({
                    "sev": vi.get("severity", "minor"),
                    "desc": desc,
                    "source": "Vision",
                    "category": vi.get("category", "ui"),
                    "fix": vi.get("location", ""),
                })
        vision_data = {
            "score": vision_score,
            "summary": vision_result.get("summary", ""),
            "suggestions": vision_result.get("suggestions", []),
            "model_used": vision_result.get("model_used", ""),
        }

    return {
        "overall": round(overall, 1),
        "scores": merged_dims,
        "issues": all_issues,
        "regressions": regressions,
        "summary": codex_norm.get("summary", "") or gemini_norm.get("summary", ""),
        "strengths": codex_norm.get("strengths", []) + gemini_norm.get("strengths", []),
        "critic_scores": critic_scores,
        "aux_count": len(aux_scores),
        "normalized_critics": {n["model"]: n for n in all_norms},  # v5.2+: 정규화된 critic 데이터 전체
        "vision": vision_data,  # VIS-004: vision critic 결과
        # P1-005: degraded trace
        "degraded": bool(_degraded_critics),
        "degraded_critics": _degraded_critics,
    }


def run_debate(debate_id, task, threshold, max_rounds, initial_solution="", claude_model="", vision_url="",
               state=None, on_complete=None, save_log_fn=None):
    """R18: state를 외부에서 주입받음. on_complete는 완료 후 콜백."""
    if state is None:
        raise ValueError("state dict must be provided")
    solution = initial_solution
    all_round_issues = []   # 라운드별 이슈 누적 (regression detection용)
    last_generator_data = None  # v5.2: rejected_alternatives 전달용
    last_critic_merged = None   # v5.2: compact context package용
    last_diagnostics = None     # v5.2: revision focus용

    try:
        for r in range(1, max_rounds + 1):
            if state.get("abort"): break
            state["round"] = r

            # ── Generator (Claude) ──
            state["phase"] = "generator"
            previously_fixed = []
            for ri, round_issues in enumerate(all_round_issues):
                for iss in round_issues:
                    if isinstance(iss, dict):
                        previously_fixed.append(f"R{ri+1}: {iss.get('desc', str(iss))}")
            prev_text = "\n".join(previously_fixed[-20:]) if previously_fixed else "None"

            if r == 1 and not initial_solution:
                prompt = GENERATOR_PROMPT.format(task=task)
            elif last_diagnostics and last_critic_merged:
                # v5.2: blocker 중심 revise (compact context package 사용)
                rev_focus = build_revision_focus(last_diagnostics, last_critic_merged)
                ctx_pkg = build_compact_context_package(
                    solution[:2000], last_critic_merged, last_diagnostics, last_generator_data
                )
                prompt = GENERATOR_IMPROVE_PROMPT_V2.format(
                    task=task, solution=solution,
                    blocking_issues=format_issues_compact(rev_focus.get("blocking_issues", [])),
                    regressions="\n".join(str(r) for r in rev_focus.get("regressions", [])) or "None",
                    worst_dimensions=", ".join(rev_focus.get("worst_dimensions", [])) or "None",
                    critic_disagreements="\n".join(ctx_pkg.get("critic_disagreements", [])) or "None",
                    alternative_views="\n".join(
                        (a.get("alternative", str(a)) if isinstance(a, dict) else str(a))
                        for a in ctx_pkg.get("alternative_views", [])
                    ) or "None",
                    preserve=", ".join(ctx_pkg.get("preserve", [])) or "None",
                    previously_fixed=prev_text,
                )
            else:
                # fallback: v5.1 방식
                issues_text = format_issues_compact(
                    all_round_issues[-1] if all_round_issues else []
                )
                prompt = GENERATOR_IMPROVE_PROMPT.format(
                    task=task, solution=solution,
                    issues=issues_text, previously_fixed=prev_text
                )

            raw = call_claude(prompt, model=claude_model)
            if state.get("abort"): break

            jd = extract_json(raw)
            if jd and "solution" in jd:
                solution = jd["solution"]
                disp = (jd.get("approach", "") or "") + "\n\n" + solution
                if jd.get("changes"):
                    disp += "\n\nChanges: " + " | ".join(jd["changes"])
            else:
                solution = raw
                disp = raw

            state["messages"].append({"role": "generator", "content": disp, "ts": datetime.now().isoformat()})
            # raw_steps에 구조화 데이터 저장
            state.setdefault("raw_steps", []).append({"role": "generator", "data": jd or {}})
            last_generator_data = jd  # v5.2: rejected_alternatives 보존

            # ── Phase 2: Multi-Critic (Codex + Gemini 병렬) ──
            state["phase"] = "critic"
            critic_merged = run_multi_critic(task, solution, prev_text, vision_url=vision_url, vision_mode="full")
            if state.get("abort"): break

            c_score = critic_merged["overall"]
            state["avg_score"] = c_score
            # P1-005: degraded trace
            if critic_merged.get("degraded"):
                state["degraded"] = True
                state["degraded_critics"] = critic_merged.get("degraded_critics", [])
            all_round_issues.append(critic_merged["issues"])

            # 표시용 포맷
            scores_str = " | ".join(f"{k}:{v}" for k, v in critic_merged["scores"].items())
            critic_scores_str = " | ".join(f"{k}:{v:.1f}" for k, v in critic_merged["critic_scores"].items())
            aux_n = critic_merged.get("aux_count", 0)
            scoring_label = f"Core*{_core_w}+Aux({aux_n})*{_aux_w}" if aux_n else "min of Codex+Gemini"
            disp = f"{c_score:.1f}/10 ({scoring_label}) [{critic_scores_str}]\n"
            disp += f"Dims: [{scores_str}]\n"
            disp += f"{critic_merged.get('summary', '')}\n"
            if critic_merged["issues"]:
                disp += "\nIssues:\n"
                for iss in critic_merged["issues"]:
                    if isinstance(iss, dict):
                        sev = iss.get("sev", "")
                        ic = {"critical": "[!!]", "major": "[!]", "minor": "[.]"}.get(sev, "[?]")
                        src = iss.get("source", "")
                        disp += f"  {ic} [{src}] {iss.get('desc', '')}\n"
                        if iss.get("fix"):
                            disp += f"     -> {iss['fix']}\n"
            if critic_merged["regressions"]:
                disp += f"\n⚠ Regressions: {critic_merged['regressions']}\n"
            if critic_merged["strengths"]:
                disp += "\nStrengths: " + " | ".join(critic_merged["strengths"][:3])
            # Vision critic 요약 표시
            if critic_merged.get("vision") and critic_merged["vision"].get("score"):
                vd = critic_merged["vision"]
                disp += f"\n\nVision UI: {vd['score']:.1f}/10 ({vd.get('model_used', '?')})"
                if vd.get("summary"):
                    disp += f" — {vd['summary']}"
                for sug in vd.get("suggestions", [])[:3]:
                    disp += f"\n  > {sug}"

            state["messages"].append({"role": "critic", "content": disp, "score": c_score, "ts": datetime.now().isoformat()})
            state.setdefault("raw_steps", []).append({"role": "critic", "data": critic_merged})

            # v5.2: diagnostics 저장 (다음 라운드 revision focus용)
            last_critic_merged = critic_merged
            last_diagnostics = check_convergence_v2(critic_merged, threshold)
            state.setdefault("raw_steps", []).append({"role": "diagnostics", "data": last_diagnostics})

            # ── 수렴 판정 (다차원) ──
            converged = last_diagnostics["converged"]
            reason = last_diagnostics.get("reason", "converged")
            if converged:
                state["status"] = "converged"
                state["final_solution"] = solution
                break

            # ── Phase 2: Synthesizer = Codex (Generator와 다른 모델) ──
            if r < max_rounds:
                state["phase"] = "synthesizer"
                issues_text = format_issues_compact(critic_merged["issues"])
                synth_raw = call_codex(SYNTHESIZER_PROMPT.format(
                    task=task, solution=solution, issues=issues_text
                ))
                if state.get("abort"): break

                synth_jd = extract_json(synth_raw)
                if synth_jd and "solution" in synth_jd:
                    solution = synth_jd["solution"]
                    disp = (synth_jd.get("approach", "") or "") + "\n"
                    if synth_jd.get("fixed"):
                        disp += "\nFixed: " + " | ".join(synth_jd["fixed"][:5])
                    if synth_jd.get("remaining"):
                        disp += "\n\nRemaining: " + " | ".join(synth_jd["remaining"][:3])
                    disp += "\n\n" + solution
                else:
                    solution = synth_raw
                    disp = synth_raw

                state["messages"].append({"role": "synthesizer", "content": disp, "model": "Codex", "ts": datetime.now().isoformat()})
                state.setdefault("raw_steps", []).append({"role": "synthesizer", "data": synth_jd or {}})

        if state["status"] == "running":
            state["status"] = "max_rounds"
            state["final_solution"] = solution

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)

    if state.get("abort"):
        state["status"] = "aborted"

    state["finished_at"] = datetime.now().isoformat()
    if save_log_fn:
        save_log_fn(LOG_DIR / f"{debate_id}.json", state)
    if on_complete and state["status"] in ("converged", "max_rounds", "completed"):
        on_complete()


# ═══════════════════════════════════════════
# AUTO SCORING TUNE — 완료 시 자동 가중치 조정
# ═══════════════════════════════════════════
_completed_count = 0
_AUTO_TUNE_INTERVAL = 10  # 10회 완료마다 자동 튜닝


def _maybe_auto_tune_scoring():
    """완료 카운트가 interval에 도달하면 scoring 가중치 자동 튜닝."""
    global _completed_count
    _completed_count += 1
    if _completed_count % _AUTO_TUNE_INTERVAL == 0:
        try:
            from core.adaptive.analytics import auto_tune_scoring_weights
            result = auto_tune_scoring_weights(dry_run=False)
            print(f"[AUTO-TUNE] scoring weights updated: core={result['core_weight']}, aux={result['aux_weight']} ({result['reason']})")
        except Exception as e:
            print(f"[AUTO-TUNE] failed: {e}")

