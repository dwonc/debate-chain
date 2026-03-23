"""
Debate Chain Web Server v7
Phase 1: subprocess 보안 수정 (shell=False + stdin 직접, temp file 제거)
Phase 2: Multi-Critic(Codex+Gemini 병렬) + Synthesizer 모델 분리 + Regression detection + 다차원 수렴
Phase 3: debate_pair 파이프라인 + self_improve + SSE 스트리밍
"""
import json
import subprocess
import os
import re
import time
import threading
import concurrent.futures
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string, Response

app = Flask(__name__)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# --- Gemini model fallback ---
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-pro",
    "gemini-1.5-flash",
]
_gemini_current_model_idx = 0
_gemini_lock = threading.Lock()

# ═══════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════

GENERATOR_PROMPT = """Task: {task}

Reply JSON only:
{{"solution":"<complete code/text>","approach":"<1 sentence>","decisions":["d1","d2"]}}"""

GENERATOR_IMPROVE_PROMPT = """Task: {task}

Current solution:
{solution}

Fix these issues:
{issues}

Previously fixed issues (do NOT regress):
{previously_fixed}

Reply JSON only:
{{"solution":"<improved complete solution>","approach":"<1 sentence>","changes":["fix1","fix2"]}}"""

# Phase 2: 다차원 수렴 Critic 프롬프트
CRITIC_PROMPT = """Task: {task}

Solution:
{solution}

Previously fixed issues (check for regressions):
{previously_fixed}

You are a ruthless code reviewer. Find EVERY flaw. Score each dimension 1-10.
Reply JSON only:
{{"scores":{{"correctness":<1-10>,"completeness":<1-10>,"security":<1-10>,"performance":<1-10>}},"overall":<1-10>,"summary":"<2 sentences>","issues":[{{"sev":"critical|major|minor","desc":"<issue>","fix":"<suggestion>"}}],"regressions":["<regressed issue if any>"],"strengths":["s1"],"on_task":true}}"""

SYNTHESIZER_PROMPT = """Task: {task}

Solution:
{solution}

Issues to fix:
{issues}

Produce improved COMPLETE solution addressing every issue.
Reply JSON only:
{{"solution":"<complete improved solution>","approach":"<1 sentence>","fixed":["issue1->fix","issue2->fix"],"remaining":["concern1"]}}"""

SPLIT_PROMPT = """Split into {num_parts} parallel parts.

Task: {task}
{extra_context}

Reply JSON only (VERY SHORT descriptions):
{{"project_name":"<n>","shared_spec":{{"interfaces":"<1 line>","notes":"<1 line>"}},"parts":[{{"id":"part1","title":"<5 words>","description":"<2 sentences max>"}}]}}"""

SPLIT_PROMPT_WITH_ARTIFACT = """Split into {num_parts} parallel parts.

Task: {task}

Debate-validated design (score {artifact_score}/10, {artifact_rounds} rounds):
{final_solution_summary}

Key decisions: {key_decisions}
Remaining concerns: {remaining_concerns}

Reply JSON only:
{{"project_name":"<n>","shared_spec":{{"interfaces":"<1 line>","constraints":"<1 line>"}},"parts":[{{"id":"part1","title":"<5 words>","description":"<2 sentences max>"}}]}}"""

PART_PROMPT = """You are an expert developer. Build this part of a larger project.

Overall task: {task}
Your part: {part_title}
Details: {part_description}

Shared spec:
{shared_spec}

{extra_context}

Write production-quality, complete code. Reply JSON only:
{{"files":[{{"path":"<file path>","code":"<complete code>"}}],"setup":"<install/run instructions>","notes":"<integration notes>"}}"""

SELF_IMPROVE_PROMPT = """Previous attempt:
{prev}

Task: {task}

Analyze your previous attempt critically.
List exactly 3 weaknesses, then produce a BETTER version fixing all of them.

Reply JSON only:
{{"weaknesses":["w1","w2","w3"],"solution":"<complete improved version>","improvements":["what changed"]}}"""

# ═══════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════

def extract_json(text):
    if not text or "[ERROR]" in text:
        return None
    cleaned = re.sub(r'```(?:json)?\s*', '', text).replace('```', '').strip()
    try:
        return json.loads(cleaned)
    except:
        pass
    depth = 0
    start = -1
    for i, c in enumerate(cleaned):
        if c == '{':
            if depth == 0: start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try: return json.loads(cleaned[start:i+1])
                except: start = -1
    return None


def format_issues_compact(issues_list):
    if not issues_list:
        return "None."
    lines = []
    for i, iss in enumerate(issues_list, 1):
        if isinstance(iss, dict):
            s = iss.get("sev", iss.get("severity", "?"))
            d = iss.get("desc", iss.get("description", str(iss)))
            fx = iss.get("fix", iss.get("suggestion", ""))
            line = f"#{i}[{s}] {d}"
            if fx: line += f" -> {fx}"
            lines.append(line)
        else:
            lines.append(f"#{i} {iss}")
    return "\n".join(lines)


def extract_score(data, raw_text):
    # Phase 2: 다차원 점수에서 overall 우선
    if data:
        for key in ("overall", "score"):
            if key in data:
                try:
                    s = float(data[key])
                    if 0 < s <= 10: return s
                except: pass
    for p in [r'"overall"\s*:\s*(\d+(?:\.\d+)?)', r'"score"\s*:\s*(\d+(?:\.\d+)?)', r'(\d+(?:\.\d+)?)\s*/\s*10']:
        m = re.search(p, raw_text or "")
        if m:
            s = float(m.group(1))
            if 0 < s <= 10: return s
    return 5.0


def check_convergence(critic_data, threshold=8.0, min_per_dim=6.0):
    """Phase 2: 다차원 수렴 판정"""
    overall = critic_data.get("overall", critic_data.get("score", 0))
    if overall < threshold:
        return False, f"overall {overall} < {threshold}"

    dims = critic_data.get("scores", {})
    for dim, val in dims.items():
        try:
            if float(val) < min_per_dim:
                return False, f"{dim} {val} < {min_per_dim}"
        except: pass

    criticals = [i for i in critic_data.get("issues", []) if isinstance(i, dict) and i.get("sev") == "critical"]
    if criticals:
        return False, f"{len(criticals)} critical issues remain"

    regressions = critic_data.get("regressions", [])
    if regressions and regressions != ["<regressed issue if any>"]:
        return False, f"regressions: {regressions}"

    return True, "converged"


def extract_debate_artifact(state: dict) -> dict:
    """Phase 3: debate 결과를 pair가 소비할 수 있는 구조화된 형태로 변환"""
    artifact = {
        "task": state.get("task", ""),
        "final_solution": state.get("final_solution", ""),
        "score": state.get("avg_score", 0),
        "rounds": state.get("round", 0),
        "key_decisions": [],
        "resolved_issues": [],
        "remaining_concerns": [],
    }
    # raw_steps에서 구조화된 데이터 추출 (Phase 2에서 저장)
    for step in state.get("raw_steps", []):
        role = step.get("role", "")
        data = step.get("data", {})
        if role == "generator" and data:
            artifact["key_decisions"].extend(data.get("decisions", []))
        elif role == "synthesizer" and data:
            artifact["resolved_issues"].extend(data.get("fixed", [])[:5])
            artifact["remaining_concerns"].extend(data.get("remaining", [])[:3])
    # fallback: messages에서 추출
    if not artifact["key_decisions"]:
        for msg in state.get("messages", []):
            if msg.get("role") == "generator":
                jd = extract_json(msg.get("content", ""))
                if jd:
                    artifact["key_decisions"].extend(jd.get("decisions", [])[:3])
    return artifact


# ═══════════════════════════════════════════
# Phase 1: AI CALLERS (shell=False + stdin, temp file 제거)
# ═══════════════════════════════════════════

# OS별 CLI 경로 자동 감지
import platform
import shutil

def _resolve_cmd(name, win_args, mac_args):
    """Windows: npm 절대경로 .cmd / Mac·Linux: PATH에서 찾기"""
    if platform.system() == "Windows":
        npm = r"C:\Users\User\AppData\Roaming\npm"
        return ["cmd", "/c", f"{npm}\\{name}.cmd"] + win_args
    else:
        # Mac/Linux: shutil.which로 PATH 탐색, 없으면 이름 그대로
        path = shutil.which(name) or name
        return [path] + mac_args

_CLAUDE_CMD  = _resolve_cmd("claude",  ["-p"],                        ["-p"])
_CODEX_CMD   = _resolve_cmd("codex",   ["exec", "--skip-git-repo-check"], ["exec", "--skip-git-repo-check"])
_GEMINI_BASE = _resolve_cmd("gemini",  [],                             [])

def call_claude(prompt, timeout=600):
    """Phase 1: shell=False, stdin 직접 전달"""
    try:
        r = subprocess.run(
            _CLAUDE_CMD,
            input=prompt,
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        out = r.stdout.strip()
        if r.returncode != 0 and not out:
            return f"[ERROR] Claude (rc={r.returncode}): {r.stderr[:500]}"
        return out if out else f"[ERROR] Claude empty: {r.stderr[:300]}"
    except subprocess.TimeoutExpired: return "[ERROR] Claude timeout"
    except FileNotFoundError: return "[ERROR] Claude CLI not found"
    except Exception as e: return f"[ERROR] Claude: {str(e)[:500]}"


def call_codex(prompt, timeout=600):
    """Phase 1: shell=False + stdin 직접 전달 (temp file 제거)
    codex exec가 stdin 지원 안 할 경우 codex -p 폴백"""
    try:
        r = subprocess.run(
            _CODEX_CMD,
            input=prompt,
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        out = r.stdout.strip()
        if r.returncode != 0 and not out:
            # stdin 지원 안 하면 -p 방식으로 폴백
            return _call_codex_fallback(prompt, timeout)
        return out if out else _call_codex_fallback(prompt, timeout)
    except FileNotFoundError: return "[ERROR] Codex CLI not found"
    except subprocess.TimeoutExpired: return "[ERROR] Codex timeout"
    except Exception as e: return f"[ERROR] Codex: {str(e)[:500]}"


def _call_codex_fallback(prompt, timeout=600):
    """codex stdin 미지원 시 temp file 폴백 (cross-platform)"""
    import tempfile
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                          encoding='utf-8', dir=str(LOG_DIR)) as f:
            f.write(prompt); tmp = f.name
        if platform.system() == "Windows":
            npm = r"C:\Users\User\AppData\Roaming\npm"
            cmd = f'type "{tmp}" | "{npm}\\codex.cmd" exec --skip-git-repo-check'
        else:
            codex_path = shutil.which("codex") or "codex"
            cmd = f'cat "{tmp}" | "{codex_path}" exec --skip-git-repo-check'
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", shell=True)
        out = r.stdout.strip()
        if r.returncode != 0 and not out:
            return f"[ERROR] Codex (rc={r.returncode}): {r.stderr[:500]}"
        return out if out else f"[ERROR] Codex empty: {r.stderr[:300]}"
    except subprocess.TimeoutExpired: return "[ERROR] Codex timeout"
    except Exception as e: return f"[ERROR] Codex fallback: {str(e)[:500]}"
    finally:
        if tmp:
            try: os.unlink(tmp)
            except: pass


def _call_gemini_with_model(prompt, model, timeout=600):
    """Phase 1: shell=False + stdin 직접 전달 (temp file 제거), model 화이트리스트 검증"""
    if model not in GEMINI_MODELS:
        return "[ERROR] Invalid Gemini model", "error"
    try:
        r = subprocess.run(
            _GEMINI_BASE + ["--model", model],
            input=prompt,
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        out = r.stdout.strip()
        stderr = r.stderr or ""
        if r.returncode != 0 and ("quota" in stderr.lower() or "exhausted" in stderr.lower()):
            return None, "quota"
        if r.returncode != 0 and not out:
            # stdin 미지원 시 temp file 폴백
            return _call_gemini_fallback(prompt, model, timeout)
        return (out if out else f"[ERROR] Gemini/{model} empty"), "ok"
    except subprocess.TimeoutExpired: return "[ERROR] Gemini timeout", "error"
    except FileNotFoundError: return "[ERROR] Gemini CLI not found", "error"
    except Exception as e: return f"[ERROR] Gemini: {str(e)[:500]}", "error"


def _call_gemini_fallback(prompt, model, timeout=600):
    """Gemini stdin 미지원 시 temp file 폴백 (cross-platform)"""
    import tempfile
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                          encoding='utf-8', dir=str(LOG_DIR)) as f:
            f.write(prompt); tmp = f.name
        if platform.system() == "Windows":
            npm = r"C:\Users\User\AppData\Roaming\npm"
            cmd = f'type "{tmp}" | "{npm}\\gemini.cmd" --model {model}'
        else:
            gemini_path = shutil.which("gemini") or "gemini"
            cmd = f'cat "{tmp}" | "{gemini_path}" --model {model}'
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", shell=True
        )
        out = r.stdout.strip()
        stderr = r.stderr or ""
        if r.returncode != 0 and ("quota" in stderr.lower() or "exhausted" in stderr.lower()):
            return None, "quota"
        if r.returncode != 0 and not out:
            return f"[ERROR] Gemini/{model}: {stderr[:500]}", "error"
        return (out if out else f"[ERROR] Gemini/{model} empty"), "ok"
    except subprocess.TimeoutExpired: return "[ERROR] Gemini timeout", "error"
    except Exception as e: return f"[ERROR] Gemini: {str(e)[:500]}", "error"
    finally:
        if tmp:
            try: os.unlink(tmp)
            except: pass


def call_gemini(prompt, timeout=600):
    global _gemini_current_model_idx
    for attempt in range(len(GEMINI_MODELS)):
        with _gemini_lock:
            idx = (_gemini_current_model_idx + attempt) % len(GEMINI_MODELS)
            model = GEMINI_MODELS[idx]
        result, status = _call_gemini_with_model(prompt, model, timeout)
        if status == "quota":
            with _gemini_lock:
                _gemini_current_model_idx = (idx + 1) % len(GEMINI_MODELS)
            continue
        if status == "ok":
            with _gemini_lock:
                _gemini_current_model_idx = idx
        return result
    return "[ERROR] Gemini: all models exhausted"


# ═══════════════════════════════════════════
# Phase 2: DEBATE ENGINE v7
# Multi-Critic(Codex+Gemini 병렬) + Synthesizer=Codex + Regression detection + 다차원 수렴
# ═══════════════════════════════════════════
debates = {}


def run_multi_critic(task, solution, previously_fixed_text):
    """Phase 2: Codex + Gemini 병렬 Critic"""
    prompt = CRITIC_PROMPT.format(
        task=task, solution=solution,
        previously_fixed=previously_fixed_text or "None (first round)"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_codex = pool.submit(call_codex, prompt)
        f_gemini = pool.submit(call_gemini, prompt)
        codex_raw = f_codex.result()
        gemini_raw = f_gemini.result()

    codex_data = extract_json(codex_raw) or {}
    gemini_data = extract_json(gemini_raw) or {}

    codex_score = extract_score(codex_data, codex_raw)
    gemini_score = extract_score(gemini_data, gemini_raw)

    # 보수적 점수: 두 Critic 중 낮은 점수
    min_score = min(codex_score, gemini_score)

    # 차원별 최소값
    merged_dims = {}
    for dim in ["correctness", "completeness", "security", "performance"]:
        vals = []
        for d in [codex_data, gemini_data]:
            v = d.get("scores", {}).get(dim)
            if v is not None:
                try: vals.append(float(v))
                except: pass
        merged_dims[dim] = min(vals) if vals else 5.0

    # 이슈 합산 + 중복 제거
    all_issues = []
    seen = set()
    for data, src in [(codex_data, "Codex"), (gemini_data, "Gemini")]:
        for iss in data.get("issues", []):
            if isinstance(iss, dict):
                key = iss.get("desc", "")[:40]
                if key not in seen:
                    seen.add(key)
                    iss_copy = dict(iss)
                    iss_copy["source"] = src
                    all_issues.append(iss_copy)

    # regression 합산
    regressions = list(set(
        (codex_data.get("regressions") or []) +
        (gemini_data.get("regressions") or [])
    ))
    regressions = [r for r in regressions if r and r != "<regressed issue if any>"]

    return {
        "overall": min_score,
        "scores": merged_dims,
        "issues": all_issues,
        "regressions": regressions,
        "summary": codex_data.get("summary", "") or gemini_data.get("summary", ""),
        "strengths": codex_data.get("strengths", []) + gemini_data.get("strengths", []),
        "critic_scores": {"Codex": codex_score, "Gemini": gemini_score},
    }


def run_debate(debate_id, task, threshold, max_rounds, initial_solution=""):
    state = debates[debate_id]
    solution = initial_solution
    all_round_issues = []   # 라운드별 이슈 누적 (regression detection용)

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
            else:
                issues_text = format_issues_compact(
                    all_round_issues[-1] if all_round_issues else []
                )
                prompt = GENERATOR_IMPROVE_PROMPT.format(
                    task=task, solution=solution,
                    issues=issues_text, previously_fixed=prev_text
                )

            raw = call_claude(prompt)
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

            state["messages"].append({"role": "generator", "content": disp})
            # raw_steps에 구조화 데이터 저장
            state.setdefault("raw_steps", []).append({"role": "generator", "data": jd or {}})

            # ── Phase 2: Multi-Critic (Codex + Gemini 병렬) ──
            state["phase"] = "critic"
            critic_merged = run_multi_critic(task, solution, prev_text)
            if state.get("abort"): break

            c_score = critic_merged["overall"]
            state["avg_score"] = c_score
            all_round_issues.append(critic_merged["issues"])

            # 표시용 포맷
            scores_str = " | ".join(f"{k}:{v}" for k, v in critic_merged["scores"].items())
            critic_scores_str = " | ".join(f"{k}:{v:.1f}" for k, v in critic_merged["critic_scores"].items())
            disp = f"{c_score:.1f}/10 (min of Codex+Gemini) [{critic_scores_str}]\n"
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

            state["messages"].append({"role": "critic", "content": disp, "score": c_score})
            state.setdefault("raw_steps", []).append({"role": "critic", "data": critic_merged})

            # ── 수렴 판정 (다차원) ──
            converged, reason = check_convergence(critic_merged, threshold)
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

                state["messages"].append({"role": "synthesizer", "content": disp, "model": "Codex"})
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
    log_file = LOG_DIR / f"{debate_id}.json"
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════
# PAIR MODE
# ═══════════════════════════════════════════
pairs = {}

AI_CALLERS = [
    ("Claude Opus 4.6", call_claude),
    ("Codex GPT-5.4", call_codex),
    ("Gemini", call_gemini),
]


def run_pair(pair_id, task, mode, extra_context="", artifact=None):
    state = pairs[pair_id]
    num_parts = 3 if mode == "pair3" else 2

    try:
        state["phase"] = "splitting"

        # Phase 3: 구조화된 아티팩트가 있으면 구조적으로 전달
        if artifact:
            split_raw = call_claude(SPLIT_PROMPT_WITH_ARTIFACT.format(
                num_parts=num_parts,
                task=task,
                artifact_score=artifact.get("score", 0),
                artifact_rounds=artifact.get("rounds", 0),
                final_solution_summary=artifact.get("final_solution", "")[:1500],
                key_decisions=", ".join(artifact.get("key_decisions", [])[:5]) or "N/A",
                remaining_concerns=", ".join(artifact.get("remaining_concerns", [])[:3]) or "None",
            ))
            ctx = ""
        else:
            ctx = ""
            if extra_context:
                # 긴 context 압축
                if len(extra_context) > 2000:
                    extra_context = extra_context[:2000] + "\n[...truncated]"
                ctx = f"\nAdditional context:\n{extra_context}"
            split_raw = call_claude(SPLIT_PROMPT.format(
                task=task, num_parts=num_parts, extra_context=ctx
            ))

        split_json = extract_json(split_raw)
        if not split_json or "parts" not in split_json:
            state["messages"].append({"role": "architect", "model": "Claude Opus 4.6", "content": split_raw})
            state["status"] = "error"
            state["error"] = "Failed to split task"
            return

        shared_spec = json.dumps(split_json.get("shared_spec", {}), indent=2)
        parts = split_json["parts"][:num_parts]
        state["spec"] = json.dumps(split_json, indent=2)
        state["messages"].append({
            "role": "architect", "model": "Claude Opus 4.6",
            "content": json.dumps(split_json, indent=2)
        })

        if state.get("abort"):
            state["status"] = "aborted"; return

        state["phase"] = "parallel_gen"
        prompts = []
        for part in parts:
            prompts.append(PART_PROMPT.format(
                task=task,
                part_title=part.get("title", part.get("id", "")),
                part_description=part.get("description", ""),
                shared_spec=shared_spec,
                extra_context=ctx,
            ))

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_parts) as pool:
            futures = []
            for i, prompt in enumerate(prompts):
                if state.get("abort"):
                    break
                ai_name, ai_fn = AI_CALLERS[i % len(AI_CALLERS)]
                futures.append((parts[i], ai_name, pool.submit(ai_fn, prompt)))

            for part, ai_name, future in futures:
                if state.get("abort"):
                    break
                raw = future.result()
                pj = extract_json(raw)
                part_id = part.get("id", part.get("title", "unknown"))
                state["messages"].append({
                    "role": part_id, "model": ai_name,
                    "title": part.get("title", ""),
                    "content": json.dumps(pj, indent=2) if pj else raw
                })
                state["results"][part_id] = pj or {"raw": raw}

        if state.get("abort"):
            state["status"] = "aborted"; return
        state["status"] = "completed"

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)

    if state.get("abort"):
        state["status"] = "aborted"
    state["finished_at"] = datetime.now().isoformat()
    log_file = LOG_DIR / f"{pair_id}.json"
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════
# Phase 3: PIPELINES
# ═══════════════════════════════════════════
pipelines = {}    # debate_pair 파이프라인
self_improves = {}  # self_improve 루프


def run_debate_pair_pipeline(pipeline_id, task, pair_mode, threshold, max_rounds):
    """Phase 3: debate → pair 자동 파이프라인"""
    state = pipelines[pipeline_id]
    try:
        # Phase 1: Debate
        debate_id = pipeline_id + "_debate"
        debates[debate_id] = {
            "id": debate_id, "task": task, "status": "running",
            "round": 0, "phase": "", "messages": [], "raw_steps": [],
            "avg_score": 0, "final_solution": "",
            "error": None, "abort": False,
            "created_at": datetime.now().isoformat(), "finished_at": None,
        }
        state["debate_id"] = debate_id
        state["phase"] = "debate"

        run_debate(debate_id, task, threshold, max_rounds)

        debate_result = debates[debate_id]
        if debate_result["status"] not in ("converged", "max_rounds"):
            state["status"] = "error"
            state["error"] = f"Debate failed: {debate_result.get('error', debate_result['status'])}"
            return

        # Phase 2: 구조화된 아티팩트 추출
        artifact = extract_debate_artifact(debate_result)
        state["phase"] = "pair"

        pair_id = pipeline_id + "_pair"
        pairs[pair_id] = {
            "id": pair_id, "task": task, "mode": pair_mode, "status": "running",
            "phase": "splitting", "messages": [], "results": {}, "spec": "",
            "error": None, "abort": False,
            "created_at": datetime.now().isoformat(), "finished_at": None,
        }
        state["pair_id"] = pair_id

        run_pair(pair_id, task, pair_mode, artifact=artifact)

        state["status"] = pairs[pair_id]["status"]
        state["finished_at"] = datetime.now().isoformat()

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        state["finished_at"] = datetime.now().isoformat()


def run_self_improve(sid, task, iterations, initial_solution=""):
    """Phase 3: 자기개선 루프"""
    state = self_improves[sid]
    solution = initial_solution
    caller = call_claude  # self_improve는 Claude 기본

    try:
        for i in range(1, iterations + 1):
            if state.get("abort"): break
            state["iteration"] = i

            if i == 1 and not initial_solution:
                raw = caller(GENERATOR_PROMPT.format(task=task))
            else:
                raw = caller(SELF_IMPROVE_PROMPT.format(prev=solution, task=task))

            jd = extract_json(raw)
            if jd:
                solution = jd.get("solution", raw)
                weaknesses = jd.get("weaknesses", [])
                improvements = jd.get("improvements", [])
            else:
                solution = raw
                weaknesses, improvements = [], []

            state["messages"].append({
                "role": f"iteration_{i}",
                "content": solution,
                "weaknesses": weaknesses,
                "improvements": improvements,
            })

        # 최종 Critic 검증 (Codex — 다른 모델)
        state["phase"] = "final_critic"
        critic_raw = call_codex(CRITIC_PROMPT.format(
            task=task, solution=solution,
            previously_fixed="None (self-improve final check)"
        ))
        critic_data = extract_json(critic_raw) or {}
        state["final_score"] = extract_score(critic_data, critic_raw)
        state["final_solution"] = solution
        state["status"] = "completed"

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)

    state["finished_at"] = datetime.now().isoformat()
    log_file = LOG_DIR / f"{sid}.json"
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/start", methods=["POST"])
def start_debate():
    data = request.json
    task = data.get("task", "").strip()
    if not task: return jsonify({"error": "task required"}), 400
    debate_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    threshold = data.get("threshold", 8.0)
    max_rounds = data.get("max_rounds", 5)
    initial_solution = data.get("initial_solution", "")

    # Deep Dive: parent_debate_id가 있으면 final_solution 자동 이어받기
    parent_id = data.get("parent_debate_id", "")
    parent_task = ""
    if parent_id:
        parent = debates.get(parent_id)
        if not parent:
            log_file = LOG_DIR / f"{parent_id}.json"
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8") as f:
                    parent = json.load(f)
        if parent:
            if not initial_solution:
                initial_solution = parent.get("final_solution", "")
            if not task or task == parent.get("task", ""):
                parent_task = parent.get("task", "")
                task = task or parent_task
    debates[debate_id] = {
        "id": debate_id, "task": task, "status": "running",
        "round": 0, "phase": "", "messages": [], "raw_steps": [],
        "avg_score": 0, "final_solution": "",
        "error": None, "abort": False,
        "parent_debate_id": parent_id or None,
        "created_at": datetime.now().isoformat(), "finished_at": None,
    }
    t = threading.Thread(target=run_debate,
                         args=(debate_id, task, threshold, max_rounds, initial_solution),
                         daemon=True)
    t.start()
    return jsonify({"debate_id": debate_id})


@app.route("/api/status/<debate_id>")
def get_status(debate_id):
    """compact metadata only"""
    state = debates.get(debate_id)
    if not state:
        log_file = LOG_DIR / f"{debate_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            debates[debate_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": state.get("id"),
        "task": state.get("task", ""),
        "status": state.get("status"),
        "round": state.get("round", 0),
        "phase": state.get("phase", ""),
        "avg_score": state.get("avg_score", 0),
        "message_count": len(state.get("messages", [])),
        "created_at": state.get("created_at"),
        "finished_at": state.get("finished_at"),
        "error": state.get("error"),
    })


@app.route("/api/result/<debate_id>")
def get_result(debate_id):
    """full result"""
    state = debates.get(debate_id)
    if not state:
        log_file = LOG_DIR / f"{debate_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            debates[debate_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify(state)


@app.route("/api/stop/<debate_id>", methods=["POST"])
def stop_debate(debate_id):
    state = debates.get(debate_id)
    if state: state["abort"] = True
    return jsonify({"ok": True})


@app.route("/api/threads")
def list_threads():
    threads = {}
    for f in sorted(LOG_DIR.glob("*.json"), reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            tid = d.get("id", f.stem)
            threads[tid] = {
                "id": tid, "task": d.get("task", "")[:80],
                "status": d.get("status", "unknown"),
                "avg_score": d.get("avg_score", 0), "round": d.get("round", 0),
                "created_at": d.get("created_at", ""),
            }
        except: pass
    for tid, d in {**debates, **pairs, **pipelines, **self_improves}.items():
        threads[tid] = {
            "id": tid, "task": d.get("task", "")[:80],
            "status": d.get("status", "unknown"),
            "avg_score": d.get("avg_score", d.get("final_score", 0)),
            "round": d.get("round", d.get("iteration", 0)),
            "created_at": d.get("created_at", ""),
        }
    return jsonify(sorted(threads.values(), key=lambda x: x.get("created_at", ""), reverse=True))


@app.route("/api/delete/<debate_id>", methods=["DELETE"])
def delete_thread(debate_id):
    debates.pop(debate_id, None)
    pairs.pop(debate_id, None)
    pipelines.pop(debate_id, None)
    self_improves.pop(debate_id, None)
    log_file = LOG_DIR / f"{debate_id}.json"
    if log_file.exists(): log_file.unlink()
    return jsonify({"ok": True})


@app.route("/api/test")
def test_connections():
    results = {}
    for name, fn in [("claude", call_claude), ("codex", call_codex)]:
        res = fn('Reply JSON only: {"status":"ok","model":"your_name"}')
        parsed = extract_json(res)
        results[name] = {
            "ok": "[ERROR]" not in res,
            "response": (json.dumps(parsed) if parsed else res[:200]),
            "json": parsed is not None,
        }
    return jsonify(results)


# ── Pair ──

@app.route("/api/pair", methods=["POST"])
def start_pair():
    data = request.json
    task = data.get("task", "").strip()
    if not task: return jsonify({"error": "task required"}), 400
    mode = data.get("mode", "pair2")
    extra_context = data.get("context", "")
    pair_id = "pair_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    pairs[pair_id] = {
        "id": pair_id, "task": task, "mode": mode, "status": "running",
        "phase": "", "messages": [], "results": {}, "spec": "",
        "error": None, "abort": False,
        "created_at": datetime.now().isoformat(), "finished_at": None,
    }
    t = threading.Thread(target=run_pair, args=(pair_id, task, mode, extra_context), daemon=True)
    t.start()
    return jsonify({"pair_id": pair_id, "mode": mode})


@app.route("/api/pair/status/<pair_id>")
def pair_status(pair_id):
    """compact metadata only"""
    state = pairs.get(pair_id)
    if not state:
        log_file = LOG_DIR / f"{pair_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            pairs[pair_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": state.get("id"),
        "status": state.get("status"),
        "phase": state.get("phase", ""),
        "mode": state.get("mode", ""),
        "parts_done": len([m for m in state.get("messages", []) if m.get("role") != "architect"]),
        "message_count": len(state.get("messages", [])),
        "created_at": state.get("created_at"),
        "finished_at": state.get("finished_at"),
        "error": state.get("error"),
    })


@app.route("/api/pair/result/<pair_id>")
def pair_result_full(pair_id):
    """full result"""
    state = pairs.get(pair_id)
    if not state:
        log_file = LOG_DIR / f"{pair_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            pairs[pair_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify(state)


@app.route("/api/pair/stop/<pair_id>", methods=["POST"])
def pair_stop(pair_id):
    state = pairs.get(pair_id)
    if state: state["abort"] = True
    return jsonify({"ok": True})


# ── Phase 3: debate_pair 파이프라인 ──

@app.route("/api/debate_pair", methods=["POST"])
def start_debate_pair():
    data = request.json
    task = data.get("task", "").strip()
    if not task: return jsonify({"error": "task required"}), 400
    pair_mode = data.get("pair_mode", "pair2")
    threshold = data.get("threshold", 8.0)
    max_rounds = data.get("max_rounds", 3)

    pipeline_id = "dp_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    pipelines[pipeline_id] = {
        "id": pipeline_id, "task": task, "status": "running",
        "phase": "debate", "debate_id": None, "pair_id": None,
        "created_at": datetime.now().isoformat(), "finished_at": None, "error": None,
    }
    t = threading.Thread(
        target=run_debate_pair_pipeline,
        args=(pipeline_id, task, pair_mode, threshold, max_rounds),
        daemon=True
    )
    t.start()
    return jsonify({"pipeline_id": pipeline_id, "status": "running"})


@app.route("/api/pipeline/status/<pipeline_id>")
def pipeline_status(pipeline_id):
    state = pipelines.get(pipeline_id)
    if not state: return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": state["id"], "status": state["status"],
        "phase": state["phase"],
        "debate_id": state.get("debate_id"),
        "pair_id": state.get("pair_id"),
        "error": state.get("error"),
    })


@app.route("/api/pipeline/result/<pipeline_id>")
def pipeline_result(pipeline_id):
    state = pipelines.get(pipeline_id)
    if not state: return jsonify({"error": "not found"}), 404
    result = dict(state)
    did = state.get("debate_id")
    pid = state.get("pair_id")
    if did and did in debates:
        result["debate"] = {
            "status": debates[did].get("status"),
            "avg_score": debates[did].get("avg_score", 0),
            "round": debates[did].get("round", 0),
            "final_solution": debates[did].get("final_solution", ""),
        }
    if pid and pid in pairs:
        result["pair"] = {
            "status": pairs[pid].get("status"),
            "messages": pairs[pid].get("messages", []),
        }
    return jsonify(result)


# ── Phase 3: self_improve ──

@app.route("/api/self_improve", methods=["POST"])
def start_self_improve():
    data = request.json
    task = data.get("task", "").strip()
    debate_id = data.get("debate_id")  # 기존 debate 결과 이어받기
    iterations = data.get("iterations", 3)

    initial_solution = ""
    if debate_id and debate_id in debates:
        dstate = debates[debate_id]
        if dstate["status"] not in ("converged", "max_rounds"):
            return jsonify({"error": "debate not finished"}), 400
        task = task or dstate["task"]
        initial_solution = dstate.get("final_solution", "")

    if not task: return jsonify({"error": "task required"}), 400

    sid = "si_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    self_improves[sid] = {
        "id": sid, "task": task, "status": "running",
        "iteration": 0, "total_iterations": iterations,
        "messages": [], "final_solution": "", "final_score": 0,
        "phase": "improving", "parent_debate": debate_id,
        "created_at": datetime.now().isoformat(), "finished_at": None,
        "abort": False, "error": None,
    }
    t = threading.Thread(
        target=run_self_improve,
        args=(sid, task, iterations, initial_solution),
        daemon=True
    )
    t.start()
    return jsonify({"self_improve_id": sid})


@app.route("/api/self_improve/status/<sid>")
def self_improve_status(sid):
    state = self_improves.get(sid)
    if not state: return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": sid, "status": state["status"],
        "iteration": state["iteration"],
        "total_iterations": state["total_iterations"],
        "final_score": state.get("final_score", 0),
        "phase": state.get("phase", ""),
    })


@app.route("/api/self_improve/result/<sid>")
def self_improve_result(sid):
    state = self_improves.get(sid)
    if not state: return jsonify({"error": "not found"}), 404
    return jsonify(state)


# ── Phase 3: SSE 스트리밍 ──

@app.route("/api/stream/<job_id>")
def stream_status(job_id):
    """SSE: debate 또는 pair 실시간 상태 스트리밍"""
    def generate():
        while True:
            state = debates.get(job_id) or pairs.get(job_id) or pipelines.get(job_id)
            if not state:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                return
            payload = {
                "status": state.get("status"),
                "round": state.get("round", 0),
                "phase": state.get("phase", ""),
                "avg_score": state.get("avg_score", 0),
                "message_count": len(state.get("messages", [])),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if state["status"] != "running":
                yield f"data: {json.dumps({'event': 'done', 'status': state['status']})}\n\n"
                return
            time.sleep(1)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ═══════════════════════════════════════════
# HTML UI
# ═══════════════════════════════════════════

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Debate Chain v7</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&family=JetBrains+Mono:wght@400;700&family=Noto+Sans+KR:wght@400;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d1a;color:#e0e0e0;font-family:'IBM Plex Sans','Noto Sans KR',sans-serif;height:100vh;overflow:hidden;display:flex}
.app{display:flex;flex:1;overflow:hidden}
.sidebar{width:280px;background:#0a0a18;border-right:1px solid #1a1a3a;display:flex;flex-direction:column;flex-shrink:0}
.sidebar-header{padding:16px;border-bottom:1px solid #1a1a3a;display:flex;align-items:center;gap:10px}
.sidebar-header h2{font-size:14px;font-weight:700;background:linear-gradient(135deg,#00e5ff,#da77f2);-webkit-background-clip:text;-webkit-text-fill-color:transparent;flex:1}
.btn-new{padding:6px 14px;background:linear-gradient(135deg,#00e5ff,#0099cc);border:none;border-radius:6px;color:#000;font-size:12px;font-weight:700;cursor:pointer}
.thread-list{flex:1;overflow-y:auto;padding:8px}.thread-list::-webkit-scrollbar{width:4px}.thread-list::-webkit-scrollbar-thumb{background:#333;border-radius:2px}
.thread-item{padding:10px 12px;border-radius:8px;cursor:pointer;margin-bottom:4px;border:1px solid transparent;transition:all .15s}
.thread-item:hover{background:#1a1a2e;border-color:#2a2a4a}.thread-item.active{background:#1a1a3a;border-color:#00e5ff44}
.thread-task{font-size:12px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:4px}
.thread-meta{display:flex;align-items:center;gap:6px;font-size:10px;color:#666}
.thread-status{display:inline-block;width:6px;height:6px;border-radius:50%}
.thread-status.running{background:#00e5ff;animation:pulse 1s infinite}.thread-status.converged{background:#69db7c}.thread-status.max_rounds{background:#ffd43b}.thread-status.error{background:#ff6b6b}.thread-status.completed{background:#69db7c}
.thread-score{font-family:'JetBrains Mono',monospace;font-weight:700}
.thread-delete{margin-left:auto;opacity:0;color:#ff6b6b;cursor:pointer;font-size:11px;padding:2px 6px;border-radius:4px}
.thread-item:hover .thread-delete{opacity:.6}.thread-delete:hover{opacity:1!important;background:#ff6b6b22}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.header{border-bottom:1px solid #1a1a3a;padding:14px 24px;display:flex;align-items:center;gap:14px;flex-shrink:0}
.header h1{font-size:18px;font-weight:700;background:linear-gradient(135deg,#00e5ff,#da77f2);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header p{font-size:11px;color:#555;letter-spacing:1px;text-transform:uppercase}
.roles{margin-left:auto;display:flex;gap:14px}.roles span{font-size:10px;font-weight:600;opacity:.6}
.content{flex:1;overflow-y:auto;padding:20px 24px}.content::-webkit-scrollbar{width:6px}.content::-webkit-scrollbar-thumb{background:#333;border-radius:3px}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:#444;gap:12px}
.empty-text{font-size:14px}.empty-sub{font-size:11px;color:#333;text-align:center;line-height:1.6}
.input-area{flex-shrink:0;border-top:1px solid #1a1a3a;padding:16px 24px;background:#0a0a16}
.input-row{display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;background:#12122a;border:1px solid #2a2a4a;border-radius:8px;color:#e0e0e0;font-size:13px;padding:10px 12px;resize:none;font-family:'IBM Plex Sans','Noto Sans KR',sans-serif;line-height:1.5;min-height:44px;max-height:120px}
textarea:focus{outline:none;border-color:#00e5ff;box-shadow:0 0 0 2px #00e5ff33}
.btn{padding:8px 20px;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;letter-spacing:.5px;white-space:nowrap}
.btn-run{background:linear-gradient(135deg,#00e5ff,#0099cc);color:#000;height:44px}.btn-run:disabled{background:#333;color:#666;cursor:not-allowed}
.btn-stop{background:#ff6b6b22;border:1px solid #ff6b6b55;color:#ff6b6b;height:44px}
.progress{margin-bottom:16px}.progress-info{display:flex;justify-content:space-between;margin-bottom:6px;font-size:11px;font-family:'JetBrains Mono',monospace}
.progress-label{color:#888}.progress-score{font-weight:700}
.progress-bar{height:3px;background:#2a2a4a;border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#00e5ff,#da77f2);border-radius:2px;transition:width .5s ease}
.msg{margin-bottom:14px;padding-left:14px;animation:fadeSlide .3s ease}
.msg-generator{border-left:3px solid #00e5ff}.msg-critic{border-left:3px solid #ff6b6b}.msg-synthesizer{border-left:3px solid #da77f2}
.msg-header{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.role-tag{display:inline-flex;align-items:center;gap:4px;border-radius:5px;padding:2px 8px;font-size:11px;font-weight:700;letter-spacing:.3px}
.role-generator{background:#00e5ff18;border:1px solid #00e5ff44;color:#00e5ff}.role-critic{background:#ff6b6b18;border:1px solid #ff6b6b44;color:#ff6b6b}
.role-synthesizer{background:#da77f218;border:1px solid #da77f244;color:#da77f2}
.score{display:inline-flex;border-radius:5px;padding:2px 8px;font-size:12px;font-weight:800;font-family:'JetBrains Mono',monospace}
.score-pass{background:#69db7c22;border:1px solid #69db7c55;color:#69db7c}.score-fail{background:#ff6b6b22;border:1px solid #ff6b6b55;color:#ff6b6b}
.msg pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.6;color:#d4d4d4;font-family:'JetBrains Mono',monospace;background:#1a1a2e;border-radius:8px;padding:14px;max-height:400px;overflow:auto;border:1px solid #2a2a4a}
.round-divider{display:flex;align-items:center;gap:12px;margin:20px 0 16px;color:#555;font-size:11px;font-family:'JetBrains Mono',monospace}
.round-divider::before,.round-divider::after{content:'';flex:1;height:1px;background:#1a1a3a}
.result{margin-top:16px;padding:16px;border-radius:10px;animation:fadeSlide .4s ease}
.result-ok{background:#69db7c0a;border:1px solid #69db7c33}.result-fail{background:#ff6b6b0a;border:1px solid #ff6b6b33}
.result-header{display:flex;align-items:center;gap:10px}.result-icon{font-size:24px}.result-title{font-size:15px;font-weight:700}.result-sub{font-size:11px;color:#888}
.btn-copy{margin-left:auto;padding:5px 14px;background:#2a2a4a;border:1px solid #3a3a5a;border-radius:6px;color:#aaa;font-size:11px;cursor:pointer}.btn-copy:hover{background:#3a3a5a;color:#ddd}
.test-btn{margin-top:12px;padding:8px 20px;background:#2a2a4a;border:1px solid #3a3a5a;border-radius:8px;color:#aaa;font-size:12px;cursor:pointer}.test-btn:hover{background:#3a3a5a;color:#ddd}
@keyframes fadeSlide{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<div class="app">
<div class="sidebar">
  <div class="sidebar-header"><h2>Debate Chain v7</h2><button class="btn-new" onclick="newThread()">+ New</button></div>
  <div class="thread-list" id="threadList"></div>
</div>
<div class="main">
  <div class="header">
    <div><h1>Debate Chain v7</h1><p>Multi-Critic · Regression · Pipeline</p></div>
    <div class="roles">
      <span style="color:#00e5ff">Claude (Gen)</span>
      <span style="color:#ff6b6b">Codex+Gemini (Critics)</span>
      <span style="color:#da77f2">Codex (Synth)</span>
    </div>
  </div>
  <div class="content" id="content">
    <div class="empty" id="emptyState">
      <div class="empty-text">New debate</div>
      <div class="empty-sub">Multi-Critic(Codex+Gemini) · Regression detection · Multidimensional convergence<br>Synthesizer=Codex (different model from Generator)</div>
      <button class="test-btn" onclick="testConnections()">Test connections</button>
      <div id="testResult" style="margin-top:12px;font-size:12px;font-family:'JetBrains Mono',monospace;max-width:500px"></div>
    </div>
    <div id="progressArea" style="display:none" class="progress">
      <div class="progress-info"><span id="progressLabel" class="progress-label"></span><span id="progressScore" class="progress-score"></span></div>
      <div class="progress-bar"><div id="progressFill" class="progress-fill" style="width:0%"></div></div>
    </div>
    <div id="messages"></div>
    <div id="resultArea"></div>
  </div>
  <div class="input-area">
    <div class="input-row">
      <textarea id="taskInput" rows="1" placeholder="Enter task... (Enter to run, Shift+Enter for newline)" oninput="autoGrow(this)"></textarea>
      <button id="btnStop" class="btn btn-stop" style="display:none" onclick="stopDebate()">Stop</button>
      <button id="btnRun" class="btn btn-run" onclick="startDebate()">Run</button>
    </div>
  </div>
</div>
</div>
<script>
const ROLES={generator:{name:"Generator(Claude)",cls:"generator"},critic:{name:"Critic(Codex+Gemini)",cls:"critic"},synthesizer:{name:"Synthesizer(Codex)",cls:"synthesizer"}};
const THRESHOLD=8.0,MAX_ROUNDS=5;
let cid=null,pt=null,lmc=0,run=false;
function autoGrow(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px'}
document.getElementById("taskInput").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();startDebate()}});
async function testConnections(){const el=$('testResult');el.innerHTML='Testing...';try{const r=await fetch("/api/test");const d=await r.json();el.innerHTML=Object.entries(d).map(([k,v])=>{const c=v.ok?'#69db7c':'#ff6b6b';return `<div style="color:${c};margin:6px 0;padding:8px;background:${c}11;border:1px solid ${c}33;border-radius:6px"><b>${v.ok?'OK':'FAIL'} ${k} ${v.json?'JSON ok':'no JSON'}</b></div>`}).join('')}catch(e){el.innerHTML=`<span style="color:#ff6b6b">${e.message}</span>`}}
async function loadThreads(){const r=await fetch("/api/threads");const t=await r.json();const el=$('threadList');if(!t.length){el.innerHTML='<div style="padding:20px;text-align:center;color:#444;font-size:12px">No debates yet</div>';return}el.innerHTML=t.map(t=>{const a=t.id===cid?'active':'';const sc=t.avg_score>=THRESHOLD?'#69db7c':t.avg_score>0?'#ff6b6b':'#666';const tm=t.created_at?new Date(t.created_at).toLocaleString('ko-KR',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';return `<div class="thread-item ${a}" onclick="selectThread('${t.id}')"><div class="thread-task">${esc(t.task)}</div><div class="thread-meta"><span class="thread-status ${t.status}"></span><span>${t.status}</span><span>R${t.round}</span><span class="thread-score" style="color:${sc}">${t.avg_score>0?t.avg_score.toFixed(1):'-'}</span><span style="margin-left:auto;color:#555">${tm}</span><span class="thread-delete" onclick="event.stopPropagation();deleteThread('${t.id}')">x</span></div></div>`}).join('')}
async function selectThread(id){if(pt)clearInterval(pt);cid=id;lmc=0;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';
  const sr=await fetch(`/api/status/${id}`);const s=await sr.json();
  if(s.error==='not found')return;
  if(s.status==='running'){
    $('taskInput').value=s.task||'';
    run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';
    pt=setInterval(poll,1500);
  } else {
    const fr=await fetch(`/api/result/${id}`);const full=await fr.json();
    $('taskInput').value=full.task||'';
    renderAll(full);
    run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';renderResult(full);
  }
  loadThreads();}
function renderAll(s){const c=$('messages');c.innerHTML='';let cr=0;(s.messages||[]).forEach(m=>{if(m.role==='generator'){cr++;c.innerHTML+=`<div class="round-divider">Round ${cr}</div>`}const r=ROLES[m.role]||{name:m.role,cls:"generator"};let sh='';if(m.score!==undefined){const p=m.score>=THRESHOLD;sh=`<span class="score ${p?'score-pass':'score-fail'}">${m.score.toFixed(1)}/10</span>`}c.innerHTML+=`<div class="msg msg-${r.cls}"><div class="msg-header"><span class="role-tag role-${r.cls}">${r.name}</span>${sh}</div><pre>${esc(m.content)}</pre></div>`});lmc=(s.messages||[]).length;sb()}
function renderResult(s){if(s.status!=='converged'&&s.status!=='max_rounds'&&s.status!=='completed')return;const ok=s.status==='converged'||s.status==='completed';const deepBtn=s.final_solution?`<button class="btn-copy" style="background:#da77f222;border-color:#da77f255;color:#da77f2" onclick="deepDive('${s.id}')">🔍 Deep Dive</button>`:'';$('resultArea').innerHTML=`<div class="result ${ok?'result-ok':'result-fail'}"><div class="result-header"><span class="result-icon">${ok?'✅':'⚠️'}</span><div><div class="result-title" style="color:${ok?'#69db7c':'#ff6b6b'}">${s.status}</div><div class="result-sub">${s.round||0} rounds · Score: ${(s.avg_score||0).toFixed(1)}/10${s.parent_debate_id?` · (child of ${s.parent_debate_id})`:''}</div></div>${deepBtn}<button class="btn-copy" onclick="copyResult()">Copy</button></div></div>`}
async function deleteThread(id){await fetch(`/api/delete/${id}`,{method:'DELETE'});if(cid===id){cid=null;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='flex';$('taskInput').value='';$('progressArea').style.display='none'}loadThreads()}
function newThread(){if(pt)clearInterval(pt);cid=null;lmc=0;run=false;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='flex';$('taskInput').value='';$('taskInput').focus();$('progressArea').style.display='none';$('btnRun').disabled=false;$('btnStop').style.display='none';loadThreads()}
async function startDebate(){const task=$('taskInput').value.trim();if(!task||run)return;run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';lmc=0;const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({task,threshold:THRESHOLD,max_rounds:MAX_ROUNDS})});const d=await r.json();cid=d.debate_id;loadThreads();pt=setInterval(poll,1500)}
async function poll(){if(!cid)return;
  const r=await fetch(`/api/status/${cid}`);const s=await r.json();
  $('progressLabel').textContent=`Round ${s.round}/${MAX_ROUNDS} - ${s.phase||'...'}`;
  $('progressFill').style.width=Math.min((s.round/MAX_ROUNDS)*100,100)+"%";
  if(s.avg_score>0){$('progressScore').textContent=`Score: ${s.avg_score.toFixed(1)} / ${THRESHOLD}`;$('progressScore').style.color=s.avg_score>=THRESHOLD?'#69db7c':'#ff6b6b'}
  if(s.status!=='running'){
    clearInterval(pt);run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';
    const fr=await fetch(`/api/result/${cid}`);const full=await fr.json();
    renderAll(full);renderResult(full);loadThreads();
  } else if(s.message_count>lmc){
    const fr=await fetch(`/api/result/${cid}`);const full=await fr.json();
    const c=$('messages');const msgs=full.messages||[];
    for(let i=lmc;i<msgs.length;i++){const m=msgs[i];if(m.role==='generator'){c.innerHTML+=`<div class="round-divider">Round ${Math.floor(i/3)+1}</div>`}const ro=ROLES[m.role]||{name:m.role,cls:"generator"};let sh='';if(m.score!==undefined){const p=m.score>=THRESHOLD;sh=`<span class="score ${p?'score-pass':'score-fail'}">${m.score.toFixed(1)}/10</span>`}c.innerHTML+=`<div class="msg msg-${ro.cls}"><div class="msg-header"><span class="role-tag role-${ro.cls}">${ro.name}</span>${sh}</div><pre>${esc(m.content)}</pre></div>`}
    lmc=msgs.length;sb();
  }}
async function stopDebate(){if(cid)await fetch(`/api/stop/${cid}`,{method:"POST"})}
async function deepDive(parentId){const parentFull=await(await fetch(`/api/result/${parentId}`)).json();const task=parentFull.task||'';const focusHint=prompt(`Deep Dive 포커스 힌트 (선택사항, Enter 스킵):\n현재 task: ${task.slice(0,80)}`)??'';const finalTask=focusHint.trim()?`${task}\n\n[Deep Dive 포커스]: ${focusHint}`:task;if(!finalTask)return;if(pt)clearInterval(pt);run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';lmc=0;$('taskInput').value=finalTask;const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({task:finalTask,threshold:THRESHOLD,max_rounds:MAX_ROUNDS,parent_debate_id:parentId})});const d=await r.json();cid=d.debate_id;loadThreads();pt=setInterval(poll,1500)}
function copyResult(){fetch(`/api/result/${cid}`).then(r=>r.json()).then(s=>{navigator.clipboard.writeText(s.final_solution||'');const b=document.querySelector('.btn-copy');if(b){b.textContent='Copied!';setTimeout(()=>b.textContent='Copy',1500)}})}
function sb(){$('content').scrollTop=$('content').scrollHeight}
function $(id){return document.getElementById(id)}
function esc(t){return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
loadThreads();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("\nDebate Chain v7")
    print("  Phase 1: subprocess shell=False + stdin (temp file 제거)")
    print("  Phase 2: Multi-Critic(Codex+Gemini) + Synthesizer=Codex + Regression + 다차원 수렴")
    print("  Phase 3: debate_pair 파이프라인 + self_improve + SSE 스트리밍")
    print(f"  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
