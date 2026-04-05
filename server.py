"""
Horcrux Web Server v8
Adaptive 단일 진입점 — /api/horcrux/run 통합 엔드포인트
External modes: Auto / Fast / Standard / Full / Parallel
Internal engines: adaptive_fast/standard/full, debate_loop, planning_pipeline, pair_generation, self_improve
"""
import json
import subprocess
import os
import re
import sys
import time
import threading
import concurrent.futures
from datetime import datetime
from pathlib import Path

# Windows cp949 콘솔에서 유니코드 출력 크래시 방지
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# .env 자동 로딩 (R03: python-dotenv 사용, 기존 수동 파싱 대체)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    # dotenv 미설치 시 최소한의 수동 로딩 (fallback)
    _env_file = Path(__file__).parent / ".env"
    if _env_file.exists():
        with open(_env_file, "r", encoding="utf-8") as _ef:
            for _line in _ef:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k, _v = _k.strip(), _v.strip()
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v

from planning_v2 import register_planning_v2_routes
from deep_refactor import inject_callers as inject_drf_callers, deep_refactors, run_deep_refactor, create_state as create_drf_state
from flask import Flask, request, jsonify, render_template_string, Response
from core.security import validate_project_dir

app = Flask(__name__)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# R12: Gemini/Claude/Codex 모든 caller는 core.llm에서 관리

# ═══════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════

GENERATOR_PROMPT = """Task: {task}

Reply JSON only:
{{"solution":"<complete code/text>","approach":"<1 sentence>","decisions":["d1","d2"],"rejected_alternatives":["considered but rejected approach 1","considered but rejected approach 2"]}}"""

GENERATOR_IMPROVE_PROMPT = """Task: {task}

Current solution:
{solution}

Fix these issues:
{issues}

Previously fixed issues (do NOT regress):
{previously_fixed}

Reply JSON only:
{{"solution":"<improved complete solution>","approach":"<1 sentence>","changes":["fix1","fix2"]}}"""

# v5.2: blocker 중심 revise 프롬프트 (compact context package 사용)
GENERATOR_IMPROVE_PROMPT_V2 = """Task: {task}

Current solution:
{solution}

## Blocking Issues (fix these FIRST)
{blocking_issues}

## Regressions (must eliminate)
{regressions}

## Worst Dimensions
{worst_dimensions}

## Critic Disagreements
{critic_disagreements}

## Alternative Approaches (consider reactivating if current approach fails)
{alternative_views}

## PRESERVE (do NOT change these — already passing)
{preserve}

## Previously fixed issues (do NOT regress)
{previously_fixed}

Focus on fixing the blocking issues first. Do NOT rewrite passing areas.
Reply JSON only:
{{"solution":"<improved complete solution>","approach":"<1 sentence>","changes":["fix1","fix2"],"rejected_alternatives":["alt considered but not used"]}}"""

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

SPLIT_PROMPT = """You are a software architect splitting a task into {num_parts} parallel implementation parts.
Do NOT read or analyze any files. Ignore the current directory.

Task: {task}
{extra_context}

CRITICAL: The shared_spec MUST be detailed enough that each part can be implemented independently without conflicts.
Include ALL of the following in shared_spec:
1. "interfaces" — exact class names, method signatures with args/return types, and dataclass/model definitions that cross part boundaries
2. "imports" — how parts should import from each other (e.g., "from config import settings", not "from config import get_config()")
3. "conventions" — naming style (snake_case/camelCase), config access pattern (singleton/function), error class naming, file structure
4. "shared_files" — files that multiple parts depend on, with their EXACT structure (assign ONE part as owner for each shared file)

Reply with JSON only. No markdown, no explanation, just the JSON object:
{{"project_name":"<name>","shared_spec":{{"interfaces":"class/function signatures that cross boundaries, with type hints","imports":"exact import statements each part must use","conventions":"config pattern, naming, error handling approach","shared_files":"which shared files exist and which part owns them"}},"parts":[{{"id":"part1","title":"<5 words>","description":"<2 sentences>","owns":"<list of files this part is responsible for>"}}]}}"""

SPLIT_PROMPT_WITH_ARTIFACT = """You are a software architect splitting a task into {num_parts} parallel parts.

Task: {task}

Debate-validated design (score {artifact_score}/10, {artifact_rounds} rounds):
{final_solution_summary}

Key decisions: {key_decisions}
Remaining concerns: {remaining_concerns}

CRITICAL: The shared_spec MUST define exact interfaces so parts integrate without conflicts.
Include: class/function signatures with type hints, exact import patterns, config access convention, error class names, and file ownership per part.

Reply JSON only:
{{"project_name":"<n>","shared_spec":{{"interfaces":"exact class/function signatures with types","imports":"exact import statements","conventions":"config, naming, error handling patterns","constraints":"from debate decisions"}},"parts":[{{"id":"part1","title":"<5 words>","description":"<2 sentences>","owns":"<files this part writes>"}}]}}"""

PART_PROMPT = """You are an expert developer. Write NEW code from scratch.
Do NOT read, reference, or check any existing files. Ignore the filesystem entirely.
Generate the complete implementation directly in the JSON response.

Overall task: {task}
Your part: {part_title}
Details: {part_description}

═══ SHARED SPEC (MUST FOLLOW EXACTLY) ═══
{shared_spec}
═══ END SHARED SPEC ═══

RULES:
1. You MUST use the exact class names, method signatures, and import patterns defined in the shared spec.
2. Do NOT rename classes, change function signatures, or use a different config access pattern than specified.
3. If the spec says "from config import settings", use exactly that — not "from config import get_config()".
4. Only write files assigned to your part. Do NOT write files owned by other parts.
5. Your code must be immediately compatible with the other parts following the same spec.

{extra_context}

Write production-quality, complete code. The `code` field must contain the FULL source code, not a placeholder.
Reply JSON only:
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

def is_caller_error(text) -> bool:
    """R08: [ERROR] 문자열 체크를 중앙화 — Phase 2에서 CallerError 예외로 전환 예정."""
    return not text or "[ERROR]" in (text or "")


def extract_json(text):
    if is_caller_error(text):
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
    """Phase 2: 다차원 수렴 판정 (레거시 — run_debate 하위호환용)"""
    result = check_convergence_v2(critic_data, threshold, min_per_dim)
    return result["converged"], result.get("reason", "converged")


# ═══════════════════════════════════════════
# v5.2: CRITIC SCHEMA NORMALIZATION + CONVERGENCE DIAGNOSTICS + REVISION FOCUS
# ═══════════════════════════════════════════

# severity 매핑 테이블
_SEV_MAP = {
    "critical": "critical", "blocker": "critical", "fatal": "critical",
    "major": "major", "high": "major", "important": "major",
    "minor": "minor", "low": "minor", "trivial": "minor", "info": "minor",
}


def normalize_critic_output(raw_data: dict, source: str = "") -> dict:
    """Step 1: 모델별 critic raw output → 공통 내부 schema 변환.
    
    모든 critic(Core+Aux)이 동일 형식으로 처리되어 집계/비교/자동화 가능.
    """
    if not raw_data or not isinstance(raw_data, dict):
        return {"model": source, "score": 5.0, "dimension_scores": {},
                "issues": [], "regressions": [], "top_fixes": [],
                "verdict": "revise", "confidence": 0.0}

    # score
    score = 5.0
    for key in ("overall", "score"):
        if key in raw_data:
            try:
                v = float(raw_data[key])
                if 0 < v <= 10: score = v; break
            except: pass

    # dimension_scores
    dim_scores = {}
    raw_dims = raw_data.get("scores", raw_data.get("dimension_scores", {}))
    for dim in ["correctness", "completeness", "security", "performance"]:
        v = raw_dims.get(dim)
        if v is not None:
            try: dim_scores[dim] = float(v)
            except: dim_scores[dim] = 5.0

    # issues 정규화
    normalized_issues = []
    raw_issues = raw_data.get("issues", [])
    for i, iss in enumerate(raw_issues):
        if not isinstance(iss, dict):
            normalized_issues.append({
                "id": f"{source}_i{i}", "severity": "major",
                "dimension": "completeness", "summary": str(iss),
                "evidence": "", "fix_hint": "", "source": source,
            })
            continue
        raw_sev = iss.get("sev", iss.get("severity", "major")).lower().strip()
        severity = _SEV_MAP.get(raw_sev, "major")
        normalized_issues.append({
            "id": f"{source}_i{i}",
            "severity": severity,
            "dimension": iss.get("dimension", "completeness"),
            "summary": iss.get("desc", iss.get("summary", iss.get("description", str(iss)))),
            "evidence": iss.get("evidence", ""),
            "fix_hint": iss.get("fix", iss.get("fix_hint", iss.get("suggestion", ""))),
            "source": source,
        })

    # regressions 정규화
    raw_reg = raw_data.get("regressions", [])
    regressions = []
    for r in raw_reg:
        if not r or r == "<regressed issue if any>":
            continue
        if isinstance(r, dict):
            regressions.append(r)
        else:
            regressions.append({"id": f"{source}_reg", "summary": str(r), "evidence": "", "fix_hint": ""})

    # top_fixes 추출 (critical → major 순 fix_hint)
    top_fixes = []
    for iss in sorted(normalized_issues, key=lambda x: {"critical": 0, "major": 1, "minor": 2}.get(x["severity"], 3)):
        hint = iss.get("fix_hint", "")
        if hint and hint not in top_fixes:
            top_fixes.append(hint)
        if len(top_fixes) >= 5:
            break

    # verdict
    has_critical = any(i["severity"] == "critical" for i in normalized_issues)
    if score >= 8.0 and not has_critical and not regressions:
        verdict = "accept"
    elif score < 4.0 or len([i for i in normalized_issues if i["severity"] == "critical"]) >= 3:
        verdict = "reject"
    else:
        verdict = "revise"

    return {
        "model": source,
        "score": round(score, 1),
        "dimension_scores": dim_scores,
        "issues": normalized_issues,
        "regressions": regressions,
        "top_fixes": top_fixes,
        "verdict": verdict,
        "confidence": round(min(score / 10.0, 1.0), 2),
        "summary": raw_data.get("summary", ""),
        "strengths": raw_data.get("strengths", []),
    }


def check_convergence_v2(critic_data, threshold=8.0, min_per_dim=6.0):
    """Step 2: 구조화된 convergence diagnostics JSON 반환.
    
    문자열 대신 failed_checks/blocking_models/blocking_dimensions/next_action_focus를
    JSON으로 반환하여 revise 프롬프트 자동 생성에 사용.
    """
    overall = critic_data.get("overall", critic_data.get("score", 0))
    try: overall = float(overall)
    except: overall = 0

    failed_checks = []
    blocking_dims = []
    blocking_models = []
    next_actions = []

    # check 1: overall threshold
    if overall < threshold:
        failed_checks.append({"check": "overall_threshold", "expected": f">= {threshold}", "actual": overall})
        next_actions.append(f"raise overall score from {overall} to >= {threshold}")

    # check 2: dimension thresholds
    dims = critic_data.get("scores", {})
    failing_dims = {}
    for dim, val in dims.items():
        try:
            v = float(val)
            if v < min_per_dim:
                failing_dims[dim] = v
                blocking_dims.append(dim)
        except: pass
    if failing_dims:
        failed_checks.append({"check": "dimension_threshold", "expected": f"all >= {min_per_dim}", "actual": failing_dims})
        for dim, val in failing_dims.items():
            next_actions.append(f"raise {dim} from {val} to >= {min_per_dim}")

    # check 3: critical issues
    issues = critic_data.get("issues", [])
    criticals = [i for i in issues if isinstance(i, dict) and
                 i.get("severity", i.get("sev", "")).lower() in ("critical", "blocker")]
    if criticals:
        failed_checks.append({"check": "critical_issues", "expected": 0, "actual": len(criticals)})
        for c in criticals[:3]:
            desc = c.get("summary", c.get("desc", str(c)))
            next_actions.append(f"resolve critical: {desc[:80]}")

    # check 4: regressions
    regressions = critic_data.get("regressions", [])
    real_reg = [r for r in regressions if r and (isinstance(r, dict) or r != "<regressed issue if any>")]
    if real_reg:
        failed_checks.append({"check": "regressions", "expected": 0, "actual": len(real_reg)})
        next_actions.append("eliminate all regressions first")

    # blocking model 식별 (core critic 중 가장 낮은 점수 모델)
    critic_scores = critic_data.get("critic_scores", {})
    if critic_scores:
        min_model = min(critic_scores, key=critic_scores.get)
        if critic_scores[min_model] < threshold:
            blocking_models.append({"model": min_model, "reason": f"score {critic_scores[min_model]} < {threshold}"})

    # preserve (통과한 차원)
    good_dims = [dim for dim, val in dims.items() if dim not in blocking_dims]
    if good_dims:
        next_actions.append(f"preserve already passing: {', '.join(good_dims)}")

    converged = len(failed_checks) == 0
    reason = "converged" if converged else failed_checks[0].get("check", "unknown")

    return {
        "converged": converged,
        "reason": reason,
        "overall_score": overall,
        "failed_checks": failed_checks,
        "blocking_models": blocking_models,
        "blocking_dimensions": blocking_dims,
        "next_action_focus": next_actions[:5],
        "passing_dimensions": good_dims if not converged else list(dims.keys()),
    }


def build_revision_focus(diagnostics, critic_merged):
    """Step 3: convergence diagnostics + critic 결과에서 blocker만 추출.
    
    전체 이슈 대신 worst dimension + blocking issues만 revise에 전달하여
    토큰 절약 + 수렴 속도 향상.
    """
    blocking_issues = []
    for iss in critic_merged.get("issues", []):
        sev = iss.get("severity", iss.get("sev", "minor"))
        if sev in ("critical", "blocker"):
            blocking_issues.append(iss)

    # critical 없으면 worst dimension의 major 이슈 추가
    if not blocking_issues:
        worst_dims = diagnostics.get("blocking_dimensions", [])
        for iss in critic_merged.get("issues", []):
            sev = iss.get("severity", iss.get("sev", "minor"))
            dim = iss.get("dimension", "")
            if sev == "major" and (dim in worst_dims or not worst_dims):
                blocking_issues.append(iss)
                if len(blocking_issues) >= 5:
                    break

    return {
        "overall_score": diagnostics.get("overall_score", 0),
        "worst_dimensions": diagnostics.get("blocking_dimensions", []),
        "blocking_issues": blocking_issues[:5],
        "regressions": critic_merged.get("regressions", []),
        "top_fixes": diagnostics.get("next_action_focus", [])[:5],
        "preserve": diagnostics.get("passing_dimensions", []),
        "instruction_style": "fix_only_blockers_first",
    }


def build_compact_context_package(solution_summary, critic_merged, diagnostics, generator_data=None):
    """Step 4: full context dump 대신 편향 완화용 compact context package 생성.
    
    solution_summary + blocking_issues + critic_disagreements + alternative_views
    + preserve + must_not_change 조합으로 anchor bias를 줄임.
    """
    # critic disagreements: 점수 차이가 큰 모델 간 의견 충돌
    disagreements = []
    critic_scores = critic_merged.get("critic_scores", {})
    if len(critic_scores) >= 2:
        scores_list = list(critic_scores.items())
        for i, (m1, s1) in enumerate(scores_list):
            for m2, s2 in scores_list[i+1:]:
                if abs(s1 - s2) >= 2.0:  # 2점 이상 차이
                    disagreements.append(f"{m1}({s1:.1f}) vs {m2}({s2:.1f}): 점수 차이 {abs(s1-s2):.1f}점")

    # issue source별 관점 차이
    source_issues = {}
    for iss in critic_merged.get("issues", []):
        src = iss.get("source", "unknown")
        if src not in source_issues:
            source_issues[src] = []
        source_issues[src].append(iss.get("summary", iss.get("desc", ""))[:60])
    for src, issues in source_issues.items():
        if issues:
            disagreements.append(f"{src}: {issues[0]}")

    # alternative_views: generator의 rejected_alternatives에서 가져오기
    alternative_views = []
    if generator_data and isinstance(generator_data, dict):
        for alt in generator_data.get("rejected_alternatives", []):
            if isinstance(alt, dict):
                alternative_views.append(alt)
            elif isinstance(alt, str) and alt:
                alternative_views.append({"alternative": alt, "source": "generator"})

    return {
        "solution_summary": solution_summary[:2000] if solution_summary else "",
        "blocking_issues": [iss.get("summary", iss.get("desc", ""))[:100]
                           for iss in critic_merged.get("issues", [])
                           if iss.get("severity", iss.get("sev", "")) in ("critical", "blocker")][:5],
        "critical_issues": [iss.get("summary", iss.get("desc", ""))[:100]
                           for iss in critic_merged.get("issues", [])
                           if iss.get("severity", iss.get("sev", "")) == "critical"][:3],
        "critic_disagreements": disagreements[:5],
        "alternative_views": alternative_views[:5],
        "preserve": diagnostics.get("passing_dimensions", []),
        "must_not_change": ["core scoring philosophy", "previously resolved issues"],
    }


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


# ===============================================
# Phase 1: AI CALLERS v8 — 타임아웃/프롬프트 크기 완전 해결
# ===============================================
# 수정사항:
# 1. Claude: -p 인자 방식으로 통일 (stdin 혼용 버그 제거 — 폴더 감지 무한대기 원인)
# 2. --dangerously-skip-permissions: 폴더 권한 프롬프트 차단
# 3. 프롬프트 자동 truncation (12000자 초과 시 압축)
# 4. 타임아웃 시 6000자로 줄여서 1회 재시도
# 5. 기본 timeout 600 → 300으로 단축
# ===============================================
import platform
import shutil

_NPM = os.environ.get("CLI_BIN_DIR", "") or (
    os.path.join(os.environ.get("APPDATA", ""), "npm")
    if platform.system() == "Windows" else "/usr/local/bin"
)
# R12: MAX_PROMPT_CHARS/MAX_PROMPT_RETRY는 core.llm에서 관리

# ── R02: 입력 sanitize ──
def _sanitize_task(task: str) -> str:
    """format-string 메타문자 이스케이프 — prompt injection 방지."""
    return task.replace("{", "{{").replace("}", "}}")


# ── R07: 로그 저장 시 비밀 스크러빙 ──
from core.security import redact as _redact_secrets

def _save_log(log_path: Path, state: dict):
    """로그 저장 — API 키/토큰 등 민감 정보 제거 후 기록."""
    raw = json.dumps(state, ensure_ascii=False, indent=2)
    scrubbed = _redact_secrets(raw)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(scrubbed)

# ── R12: AI Callers → core/llm로 추출 ──
from core.llm import (
    call_claude, call_codex, call_gemini, call_gemini_fast,
    _call_aux_critic, _truncate_prompt, _truncate_for_aux,
    CLAUDE_MODELS, GEMINI_MODELS, GEMINI_FAST_MODELS,
    AUX_CRITIC_ENDPOINTS, AUX_MAX_PROMPT_CHARS,
    MAX_PROMPT_CHARS as _LLM_MAX_PROMPT, MAX_PROMPT_RETRY as _LLM_MAX_RETRY,
)




# ═══════════════════════════════════════════
# DEBATE ENGINE v7 — Global State
# ═══════════════════════════════════════════
debates = {}

# ── R15: scoring config 캐시 ──
_scoring_cache = {}
_scoring_cache_ts = 0

def _get_scoring_weights() -> tuple:
    """config.json의 scoring weights를 캐시 (60초 TTL)."""
    global _scoring_cache, _scoring_cache_ts
    now = time.time()
    if now - _scoring_cache_ts < 60 and _scoring_cache:
        return _scoring_cache.get("core_weight", 0.8), _scoring_cache.get("aux_weight", 0.2)
    try:
        with open(Path(__file__).parent / "config.json", "r", encoding="utf-8") as f:
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
        _CRITIC_TIMEOUT = 180  # seconds
        try:
            codex_raw = f_codex.result(timeout=_CRITIC_TIMEOUT)
        except Exception as e:
            codex_raw = f"[ERROR] Codex critic timeout/error: {e}"
        try:
            gemini_raw = f_gemini.result(timeout=_CRITIC_TIMEOUT)
        except Exception as e:
            gemini_raw = f"[ERROR] Gemini critic timeout/error: {e}"
        aux_results = []
        for f in aux_futures:
            try:
                aux_results.append(f.result(timeout=_CRITIC_TIMEOUT))
            except Exception as e:
                aux_results.append(("aux_timeout", f"[ERROR] Aux critic timeout: {e}"))
        try:
            vision_result = f_vision.result(timeout=_CRITIC_TIMEOUT) if f_vision else None
        except Exception as e:
            vision_result = None

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
    }


def run_debate(debate_id, task, threshold, max_rounds, initial_solution="", claude_model="", vision_url=""):
    state = debates[debate_id]
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
            critic_merged = run_multi_critic(task, solution, prev_text, vision_url=vision_url, vision_mode="full_horcrux")
            if state.get("abort"): break

            c_score = critic_merged["overall"]
            state["avg_score"] = c_score
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
    _save_log(LOG_DIR / f"{debate_id}.json", state)

    if state["status"] in ("converged", "max_rounds", "completed"):
        _maybe_auto_tune_scoring()


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


# ═══════════════════════════════════════════
# PAIR MODE
# ═══════════════════════════════════════════
pairs = {}


def _save_pair_files(results: dict, output_dir: str) -> list:
    """
    pair 결과에서 files 배열 파싱 → output_dir 기준으로 자동 저장.
    """
    base = Path(output_dir)
    saved = []
    for part_id, result in results.items():
        if not isinstance(result, dict):
            continue
        files = result.get("files", [])
        if not files and "raw" in result:
            parsed = extract_json(result["raw"])
            if parsed:
                files = parsed.get("files", [])
        for f in files:
            rel_path = f.get("path", "")
            code     = f.get("code", "")
            if not rel_path or not code:
                continue
            target = base / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(code, encoding="utf-8")
            saved.append(str(target))
            print(f"[pair] 저장됨: {target}")
    return saved


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
            state["error"] = f"Failed to split task. Claude raw response: {split_raw[:500]}"
            state["finished_at"] = datetime.now().isoformat()
            # early return 시에도 로그 저장
            try:
                log_file = LOG_DIR / f"{pair_id}.json"
                _save_log(log_file, state)
            except Exception: pass
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

        PAIR_TIMEOUT = 1200  # 20min per part (code gen is heavy)
        PAIR_RETRY_CALLERS = [
            ("Claude", call_claude),
            ("Codex", call_codex),
            ("Gemini", call_gemini),
        ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_parts) as pool:
            futures = []
            for i, prompt in enumerate(prompts):
                if state.get("abort"):
                    break
                ai_name, ai_fn = AI_CALLERS[i % len(AI_CALLERS)]
                futures.append((parts[i], ai_name, ai_fn, prompt, pool.submit(ai_fn, prompt)))

            for part, ai_name, ai_fn, prompt, future in futures:
                if state.get("abort"):
                    break
                part_id = part.get("id", part.get("title", "unknown"))
                raw = None
                used_model = ai_name

                # 1차 시도 (timeout 포함)
                try:
                    raw = future.result(timeout=PAIR_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    print(f"[PAIR] {ai_name} timed out for {part_id}, retrying...")
                    raw = None
                except Exception as e:
                    print(f"[PAIR] {ai_name} error for {part_id}: {e}")
                    raw = None

                # 타임아웃/실패 시 다른 모델로 재시도
                if not raw or "[ERROR]" in (raw or ""):
                    for retry_name, retry_fn in PAIR_RETRY_CALLERS:
                        if retry_name == ai_name:
                            continue  # 같은 모델 스킵
                        print(f"[PAIR] Retrying {part_id} with {retry_name}...")
                        try:
                            raw = retry_fn(prompt, timeout=PAIR_TIMEOUT)
                            if raw and "[ERROR]" not in raw:
                                used_model = f"{retry_name} (retry)"
                                break
                        except Exception:
                            continue

                # 결과 저장 (실패해도 부분 결과 보존)
                pj = extract_json(raw) if raw else None
                status_label = "ok" if pj else "raw" if raw else "failed"
                state["messages"].append({
                    "role": part_id, "model": used_model,
                    "title": part.get("title", ""),
                    "status": status_label,
                    "content": json.dumps(pj, indent=2) if pj else (raw or f"[FAILED] {ai_name} and all retries failed for {part_id}")
                })
                state["results"][part_id] = pj or {"raw": raw or "", "status": status_label}

        if state.get("abort"):
            state["status"] = "aborted"; return
        state["status"] = "completed"

        # ── 자동 파일 저장 ──
        output_dir = state.get("output_dir", "")
        if output_dir:
            _save_pair_files(state["results"], output_dir)

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)

    if state.get("abort"):
        state["status"] = "aborted"
    state["finished_at"] = datetime.now().isoformat()
    log_file = LOG_DIR / f"{pair_id}.json"
    _save_log(log_file, state)

    if state["status"] == "completed":
        _maybe_auto_tune_scoring()


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
    _save_log(log_file, state)

    if state["status"] == "completed":
        _maybe_auto_tune_scoring()


# ═══════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)



@app.route("/api/start", methods=["POST"])
def start_debate():
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
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
    # project_dir 지정 시 프로젝트 코드를 읽어 task에 context로 첨부 (R06: 경로 검증)
    project_dir = data.get("project_dir", "")
    if project_dir:
        try:
            validate_project_dir(project_dir)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        project_code = _read_project_files(project_dir)
        if project_code:
            task = (
                f"{task}\n\n"
                f"=== 현재 프로젝트 코드 ({project_dir}) ===\n"
                f"{project_code}\n"
                f"=== 위 코드를 분석하여 \uace0도화 포인트를 판단하라 ==="
            )

    debates[debate_id] = {
        "id": debate_id, "task": task, "status": "running",
        "round": 0, "phase": "", "messages": [], "raw_steps": [],
        "avg_score": 0, "final_solution": "",
        "error": None, "abort": False,
        "parent_debate_id": parent_id or None,
        "project_dir": project_dir,
        "created_at": datetime.now().isoformat(), "finished_at": None,
    }
    claude_model = CLAUDE_MODELS.get(data.get("claude_model", ""), "")
    debates[debate_id]["claude_model"] = claude_model

    t = threading.Thread(target=run_debate,
                         args=(debate_id, task, threshold, max_rounds, initial_solution, claude_model, data.get("vision_url", "")),
                         daemon=True)
    t.start()
    return jsonify({"debate_id": debate_id, "project_dir": project_dir, "claude_model": claude_model or "default"})


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
    from planning_v2 import plannings as plan_v2_states
    for tid, d in {**debates, **pairs, **pipelines, **self_improves, **plan_v2_states, **horcrux_states, **deep_refactors}.items():
        threads[tid] = {
            "id": tid, "task": d.get("task", "")[:80],
            "status": d.get("status", "unknown"),
            "avg_score": d.get("avg_score", d.get("final_score", 0)),
            "round": d.get("round", d.get("iteration", 0)),
            "created_at": d.get("created_at", ""),
        }
    return jsonify(sorted(threads.values(), key=lambda x: x.get("created_at", ""), reverse=True))


@app.route("/api/timing/<job_id>")
def get_timing(job_id):
    """job 전체 소요시간 + phase별 breakdown"""
    state = (debates.get(job_id) or pairs.get(job_id)
             or pipelines.get(job_id) or self_improves.get(job_id))
    if not state:
        for store in [debates, pairs, pipelines, self_improves]:
            log_file = LOG_DIR / f"{job_id}.json"
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                break
    if not state:
        return jsonify({"error": "not found"}), 404

    created = state.get("created_at")
    finished = state.get("finished_at")
    total_sec = None
    if created and finished:
        from datetime import timezone
        def parse_dt(s):
            return datetime.fromisoformat(s)
        try:
            total_sec = round((parse_dt(finished) - parse_dt(created)).total_seconds(), 1)
        except: pass

    # message timestamp에서 phase별 소요시간 계산
    phases = []
    msgs = state.get("messages", [])
    for i, m in enumerate(msgs):
        ts = m.get("ts")
        next_ts = msgs[i+1].get("ts") if i+1 < len(msgs) else finished
        if ts and next_ts:
            try:
                dur = round((datetime.fromisoformat(next_ts) - datetime.fromisoformat(ts)).total_seconds(), 1)
                phases.append({"role": m["role"], "round": (i // 3) + 1, "duration_sec": dur, "ts": ts})
            except: pass

    return jsonify({
        "id": job_id,
        "status": state.get("status"),
        "created_at": created,
        "finished_at": finished,
        "total_sec": total_sec,
        "total_min": round(total_sec / 60, 1) if total_sec else None,
        "rounds": state.get("round", 0),
        "avg_score": state.get("avg_score", 0),
        "phase_breakdown": phases,
    })


@app.route("/api/delete/<debate_id>", methods=["DELETE"])
def delete_thread(debate_id):
    from planning_v2 import plannings as plan_v2_states
    debates.pop(debate_id, None)
    pairs.pop(debate_id, None)
    pipelines.pop(debate_id, None)
    self_improves.pop(debate_id, None)
    plan_v2_states.pop(debate_id, None)
    log_file = LOG_DIR / f"{debate_id}.json"
    if log_file.exists(): log_file.unlink()
    return jsonify({"ok": True})


# ── Vision UI Critic API ──

@app.route("/api/vision/analyze", methods=["POST"])
def vision_analyze():
    """
    이미지 파일 업로드 또는 URL → Vision UI Critic 분석.

    사용법 1 — 이미지 업로드:
      curl -X POST localhost:5000/api/vision/analyze -F "image=@screenshot.png"

    사용법 2 — URL 캡처 후 분석:
      curl -X POST localhost:5000/api/vision/analyze -H "Content-Type: application/json" \
           -d '{"url": "http://localhost:3000", "viewport": "desktop"}'

    사용법 3 — base64 직접 전달:
      curl -X POST localhost:5000/api/vision/analyze -H "Content-Type: application/json" \
           -d '{"image_base64": "iVBOR...", "viewport": "mobile"}'
    """
    from core.vision.critic import vision_ui_critic, run_vision_critic, analyze_image_file
    import base64 as _b64

    viewport = "desktop"
    rules_path = None

    # ── 방법 1: multipart 이미지 업로드 ──
    if "image" in request.files:
        f = request.files["image"]
        viewport = request.form.get("viewport", "desktop")
        rules_path = request.form.get("rules_path") or None
        img_bytes = f.read()
        img_b64 = _b64.b64encode(img_bytes).decode("ascii")
        result = vision_ui_critic(image_base64=img_b64, viewport=viewport, rules_path=rules_path)
        result["source"] = "upload"
        result["filename"] = f.filename
        return jsonify(result)

    # ── 방법 2/3: JSON body ──
    data = request.json or {}
    viewport = data.get("viewport", "desktop")
    rules_path = data.get("rules_path") or None
    color_scheme = data.get("color_scheme", "light")

    # base64 직접 전달
    if data.get("image_base64"):
        result = vision_ui_critic(image_base64=data["image_base64"], viewport=viewport, rules_path=rules_path)
        result["source"] = "base64"
        return jsonify(result)

    # URL 캡처 후 분석
    if data.get("url"):
        result = run_vision_critic(
            url=data["url"], viewport=viewport,
            color_scheme=color_scheme, rules_path=rules_path,
        )
        result["source"] = "url_capture"
        return jsonify(result)

    return jsonify({"error": "image file, image_base64, or url required"}), 400


@app.route("/api/vision/analyze/responsive", methods=["POST"])
def vision_analyze_responsive():
    """
    URL → 3종 뷰포트(desktop/tablet/mobile) 일괄 캡처 + 분석.

    curl -X POST localhost:5000/api/vision/analyze/responsive \
         -H "Content-Type: application/json" \
         -d '{"url": "http://localhost:3000"}'
    """
    from core.vision.capture import capture_responsive
    from core.vision.critic import vision_ui_critic

    data = request.json or {}
    url = data.get("url", "")
    if not url:
        return jsonify({"error": "url required"}), 400

    color_scheme = data.get("color_scheme", "light")
    rules_path = data.get("rules_path") or None

    captures = capture_responsive(url=url, color_scheme=color_scheme, healthcheck=True)

    results = {}
    for vp_name, cap in captures.items():
        if not cap["ok"]:
            results[vp_name] = {"ok": False, "error": cap["error"]}
            continue
        critique = vision_ui_critic(
            image_base64=cap["png_base64"], viewport=vp_name, rules_path=rules_path,
        )
        critique["viewport"] = vp_name
        results[vp_name] = critique

    return jsonify({"url": url, "color_scheme": color_scheme, "results": results})


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


# ── Project-aware debate ──

def _read_project_files(project_dir: str, max_chars: int = 8000) -> str:
    """
    project_dir 아래 .py 파일을 읽어서 텍스트로 반환.
    max_chars 초과 시 파일 크기 순으로 중요도 높은 것만 포함.
    """
    base = Path(project_dir)
    if not base.exists():
        return ""

    # .py 파일 수집 (테스트/캐시 제외)
    py_files = []
    for f in base.rglob("*.py"):
        if any(p in f.parts for p in ["__pycache__", ".venv", "test", "tests", "migrations"]):
            continue
        py_files.append(f)

    # 크기 순 정렬 (큰 파일 = 핵심 로직)
    py_files.sort(key=lambda f: f.stat().st_size, reverse=True)

    chunks = []
    total = 0
    for f in py_files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            rel = str(f.relative_to(base))
            entry = f"\n### {rel}\n{content}"
            if total + len(entry) > max_chars:
                break
            chunks.append(entry)
            total += len(entry)
        except Exception:
            continue

    return "\n".join(chunks)


# ── Pair ──

@app.route("/api/pair", methods=["POST"])
def start_pair():
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
    if not task: return jsonify({"error": "task required"}), 400
    mode = data.get("mode", "pair2")
    extra_context = data.get("context", "")
    output_dir = data.get("output_dir", "")  # 자동 파일 저장 경로
    pair_id = "pair_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    pairs[pair_id] = {
        "id": pair_id, "task": task, "mode": mode, "status": "running",
        "phase": "", "messages": [], "results": {}, "spec": "",
        "output_dir": output_dir,
        "error": None, "abort": False,
        "created_at": datetime.now().isoformat(), "finished_at": None,
    }
    t = threading.Thread(target=run_pair, args=(pair_id, task, mode, extra_context), daemon=True)
    t.start()
    return jsonify({"pair_id": pair_id, "mode": mode, "output_dir": output_dir})


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
    task = _sanitize_task(data.get("task", "").strip())
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
    task = _sanitize_task(data.get("task", "").strip())
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
<title>Horcrux v7</title>
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
.msg pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.6;color:#d4d4d4;font-family:'JetBrains Mono',monospace;background:#1a1a2e;border-radius:8px;padding:14px;max-height:800px;overflow:auto;border:1px solid #2a2a4a}
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
  <div class="sidebar-header"><h2>Horcrux v7</h2><button class="btn-new" onclick="newThread()">+ New</button></div>
  <div class="thread-list" id="threadList"></div>
</div>
<div class="main">
  <div class="header">
    <div><h1>Horcrux v7</h1><p>Multi-Critic · Regression · Pipeline</p></div>
    <div class="roles" id="rolesInfo">
      <span style="color:#00e5ff">Claude (Gen)</span>
      <span style="color:#ff6b6b">Codex+Gemini+Aux (Critics)</span>
      <span style="color:#da77f2">Codex (Synth)</span>
    </div>
  </div>
  <div class="content" id="content">
    <div class="empty" id="emptyState">
      <div class="empty-text" id="emptyTitle">New debate</div>
      <div class="empty-sub" id="emptySub">Multi-Critic(Codex+Gemini) · Regression detection · Multidimensional convergence<br>Synthesizer=Codex (different model from Generator)</div>
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
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
      <div style="display:flex;gap:4px;margin-right:8px">
        <button id="modeAuto" class="btn" onclick="setMode('auto')" style="padding:4px 12px;font-size:11px;background:linear-gradient(135deg,#bc8cff,#58a6ff);color:#000">Auto</button>
        <button id="modeFast" class="btn" onclick="setMode('fast')" style="padding:4px 12px;font-size:11px;background:#2a2a4a;color:#888;border:1px solid #3a3a5a">Fast</button>
        <button id="modeStandard" class="btn" onclick="setMode('standard')" style="padding:4px 12px;font-size:11px;background:#2a2a4a;color:#888;border:1px solid #3a3a5a">Standard</button>
        <button id="modeFull" class="btn" onclick="setMode('full')" style="padding:4px 12px;font-size:11px;background:#2a2a4a;color:#888;border:1px solid #3a3a5a">Full</button>
        <button id="modeParallel" class="btn" onclick="setMode('parallel')" style="padding:4px 12px;font-size:11px;background:#2a2a4a;color:#888;border:1px solid #3a3a5a">Parallel</button>
      </div>
      <label style="font-size:11px;color:#888;font-weight:600">Claude</label>
      <select id="modelSelect" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:12px;padding:4px 8px;font-family:'JetBrains Mono',monospace;cursor:pointer">
        <option value="">Auto (default)</option>
        <option value="opus">Opus 4.6</option>
        <option value="sonnet">Sonnet 4.6</option>
      </select>
      <div id="autoOpts" style="display:flex;gap:8px;align-items:center;margin-left:4px">
        <select id="autoScope" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Scope">
          <option value="auto">Scope: Auto</option>
          <option value="small">Small</option>
          <option value="medium">Medium</option>
          <option value="large">Large</option>
        </select>
        <select id="autoRisk" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Risk">
          <option value="auto">Risk: Auto</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
        <select id="autoArtifact" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Artifact">
          <option value="none">No Artifact</option>
          <option value="ppt">PPT</option>
          <option value="pdf">PDF</option>
          <option value="doc">Doc</option>
        </select>
      </div>
      <div id="parallelOpts" style="display:none;gap:8px;align-items:center;margin-left:4px">
        <select id="parallelParts" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Parts">
          <option value="2">2 AI</option>
          <option value="3">3 AI</option>
        </select>
        <input id="outputDir" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px;width:140px" placeholder="Output Dir (optional)">
      </div>
      <div id="fullOpts" style="display:none;gap:8px;align-items:center;margin-left:4px">
        <select id="fullArtifact" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Artifact">
          <option value="none">No Artifact</option>
          <option value="ppt">PPT</option>
          <option value="pdf">PDF</option>
          <option value="doc">Doc</option>
        </select>
        <input id="fullAudience" value="general" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px;width:70px" placeholder="Audience">
        <select id="fullTone" style="background:#12122a;border:1px solid #2a2a4a;border-radius:6px;color:#e0e0e0;font-size:11px;padding:4px 6px" title="Tone">
          <option value="professional">Pro</option>
          <option value="casual">Casual</option>
          <option value="technical">Tech</option>
        </select>
      </div>
    </div>
    <div class="input-row">
      <textarea id="taskInput" rows="1" placeholder="Enter task... (Enter to run, Shift+Enter for newline)" oninput="autoGrow(this)"></textarea>
      <button id="btnStop" class="btn btn-stop" style="display:none" onclick="stopRun()">Stop</button>
      <button id="btnRun" class="btn btn-run" onclick="startRun()">Run</button>
    </div>
  </div>
</div>
</div>
<script>
const ROLES={generator:{name:"Generator(Claude)",cls:"generator"},critic:{name:"Critic(Codex+Gemini+Aux)",cls:"critic"},synthesizer:{name:"Synthesizer(Codex)",cls:"synthesizer"},final:{name:"Final Polish(Codex)",cls:"synthesizer"}};
const THRESHOLD=8.0,MAX_ROUNDS=5;
let cid=null,pt=null,lmc=0,run=false,curMode='auto';
function autoGrow(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px'}
document.getElementById("taskInput").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();startRun()}});
async function testConnections(){const el=$('testResult');el.innerHTML='Testing...';try{const r=await fetch("/api/test");const d=await r.json();el.innerHTML=Object.entries(d).map(([k,v])=>{const c=v.ok?'#69db7c':'#ff6b6b';return `<div style="color:${c};margin:6px 0;padding:8px;background:${c}11;border:1px solid ${c}33;border-radius:6px"><b>${v.ok?'OK':'FAIL'} ${k} ${v.json?'JSON ok':'no JSON'}</b></div>`}).join('')}catch(e){el.innerHTML=`<span style="color:#ff6b6b">${e.message}</span>`}}
async function loadThreads(){const r=await fetch("/api/threads");const t=await r.json();const el=$('threadList');if(!t.length){el.innerHTML='<div style="padding:20px;text-align:center;color:#444;font-size:12px">No tasks yet</div>';return}el.innerHTML=t.map(t=>{const a=t.id===cid?'active':'';const sc=t.avg_score>=THRESHOLD?'#69db7c':t.avg_score>0?'#ff6b6b':'#666';const tm=t.created_at?new Date(t.created_at).toLocaleString('ko-KR',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';return `<div class="thread-item ${a}" onclick="selectThread('${t.id}')"><div class="thread-task">${esc(t.task)}</div><div class="thread-meta"><span class="thread-status ${t.status}"></span><span>${t.status}</span><span>R${t.round}</span><span class="thread-score" style="color:${sc}">${t.avg_score>0?t.avg_score.toFixed(1):'-'}</span><span style="margin-left:auto;color:#555">${tm}</span><span class="thread-delete" onclick="event.stopPropagation();deleteThread('${t.id}')">x</span></div></div>`}).join('')}
function statusUrl(id){if(id.startsWith('plan_'))return`/api/planning/status/${id}`;if(id.startsWith('pair_'))return`/api/pair/status/${id}`;if(id.startsWith('dp_'))return`/api/pipeline/status/${id}`;if(id.startsWith('si_'))return`/api/self_improve/status/${id}`;if(id.startsWith('drf_'))return`/api/horcrux/status/${id}`;if(id.startsWith('hrx_'))return`/api/horcrux/status/${id}`;if(id.startsWith('adp_'))return`/api/horcrux/status/${id}`;return`/api/status/${id}`}
function resultUrl(id){if(id.startsWith('plan_'))return`/api/planning/result/${id}`;if(id.startsWith('pair_'))return`/api/pair/result/${id}`;if(id.startsWith('dp_'))return`/api/pipeline/result/${id}`;if(id.startsWith('si_'))return`/api/self_improve/result/${id}`;if(id.startsWith('hrx_'))return`/api/horcrux/result/${id}`;if(id.startsWith('adp_'))return`/api/horcrux/result/${id}`;return`/api/result/${id}`}
async function selectThread(id){if(pt)clearInterval(pt);cid=id;lmc=0;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';
  const isP=id.startsWith('plan_');const isPair=id.startsWith('pair_');const isHrx=id.startsWith('hrx_')||id.startsWith('adp_');setMode(isP||isHrx?'auto':isPair?'parallel':'auto');
  const sr=await fetch(statusUrl(id));const s=await sr.json();
  if(!sr.ok||s.error==='not found'){loadThreads();return}
  if(s.status==='running'){
    $('taskInput').value=s.task||'';
    run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';
    pt=setInterval(poll,1500);
  } else {
    const fr=await fetch(resultUrl(id));const full=await fr.json();
    $('taskInput').value=full.task||'';
    renderAll(full);
    run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';renderResult(full);
  }
  loadThreads();}
function renderAll(s){const c=$('messages');c.innerHTML='';let cr=0;const isPlanning=!!s.final_plan||(s.id||'').startsWith('plan_');const isPair=(s.id||'').startsWith('pair_');(s.messages||[]).forEach(m=>{if(isPair){const pairRoles={architect:{name:'Architect (Claude)',cls:'synthesizer'},part1:{name:'Part 1',cls:'generator'},part2:{name:'Part 2',cls:'critic'},part3:{name:'Part 3',cls:'synthesizer'}};const pr=pairRoles[m.role]||{name:m.role,cls:'generator'};const label=m.label||pr.name;const statusTag=m.status?` <span style="font-size:10px;color:${m.status==='ok'?'#69db7c':'#ffd43b'}">[${m.status}]</span>`:'';const modelTag=m.model?` <span style="font-size:10px;color:#555">(${m.model})</span>`:'';if(m.role==='architect'){c.innerHTML+=`<div class="round-divider">Task Splitting</div>`}else if(m.role==='part1'){c.innerHTML+=`<div class="round-divider">Parallel Generation</div>`}c.innerHTML+=`<div class="msg msg-${pr.cls}"><div class="msg-header"><span class="role-tag role-${pr.cls}">${label}</span>${modelTag}${statusTag}</div><pre>${esc(m.content)}</pre></div>`;return}if(!isPlanning&&m.role==='generator'){cr++;c.innerHTML+=`<div class="round-divider">Round ${cr}</div>`}if(isPlanning){const phaseMap={generator:'Phase 1: Generate',synthesizer:'Phase 2: Synthesize',critic:'Phase 3: Critique',final:'Phase 4: Final Polish'};const ph=phaseMap[m.role];if(ph&&!c.innerHTML.includes(ph)){c.innerHTML+=`<div class="round-divider">${ph}</div>`}}const label=m.label||((ROLES[m.role]||{}).name)||m.role;const cls=(ROLES[m.role]||{cls:'generator'}).cls;let sh='';if(m.score!==undefined){const p=m.score>=THRESHOLD;sh=`<span class="score ${p?'score-pass':'score-fail'}">${Number(m.score).toFixed(1)}/10</span>`}const modelTag=m.model?` <span style="font-size:10px;color:#555">(${m.model})</span>`:'';c.innerHTML+=`<div class="msg msg-${cls}"><div class="msg-header"><span class="role-tag role-${cls}">${label}</span>${modelTag}${sh}</div><pre>${esc(m.content)}</pre></div>`});lmc=(s.messages||[]).length;sb()}
function renderResult(s){if(s.status!=='converged'&&s.status!=='max_rounds'&&s.status!=='completed')return;const ok=s.status==='converged'||s.status==='completed';const isP=(s.id||'').startsWith('plan_');const isPair=(s.id||'').startsWith('pair_');const isDebate=!isP&&!isPair&&!(s.id||'').startsWith('hrx_')&&!(s.id||'').startsWith('si_');const deepBtn=(isDebate&&s.final_solution)?`<button class="btn-copy" style="background:#da77f222;border-color:#da77f255;color:#da77f2" onclick="deepDive('${s.id}')">Deep Dive</button>`:'';let sub;if(isPair){const partCount=Object.keys(s.results||{}).length;sub=`${s.mode||'pair2'} · ${partCount} parts generated · Parallel speed`}else if(isP){sub=`4 phases · Avg critic: ${(s.avg_score||0).toFixed(1)}/10`}else{sub=`${s.round||0} rounds · Score: ${(s.avg_score||0).toFixed(1)}/10${s.parent_debate_id?` · (child of ${s.parent_debate_id})`:''}`}$('resultArea').innerHTML=`<div class="result ${ok?'result-ok':'result-fail'}"><div class="result-header"><span class="result-icon">${ok?'✅':'⚠️'}</span><div><div class="result-title" style="color:${ok?'#69db7c':'#ff6b6b'}">${isPair?(s.mode||'Pair')+' '+s.status:isP?'Planning '+s.status:s.status}</div><div class="result-sub">${sub}</div></div>${deepBtn}<button class="btn-copy" onclick="copyResult()">Copy</button></div></div>`}
async function deleteThread(id){await fetch(`/api/delete/${id}`,{method:'DELETE'});if(cid===id){cid=null;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='flex';$('taskInput').value='';$('progressArea').style.display='none'}loadThreads()}
function newThread(){if(pt)clearInterval(pt);cid=null;lmc=0;run=false;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='flex';$('taskInput').value='';$('taskInput').focus();$('progressArea').style.display='none';$('btnRun').disabled=false;$('btnStop').style.display='none';loadThreads()}
function setMode(m){curMode=m;const modes=['auto','fast','standard','full','parallel'];const btnMap={auto:'modeAuto',fast:'modeFast',standard:'modeStandard',full:'modeFull',parallel:'modeParallel'};const gradients={auto:'linear-gradient(135deg,#bc8cff,#58a6ff)',fast:'linear-gradient(135deg,#3fb950,#2ea043)',standard:'linear-gradient(135deg,#d29922,#e3b341)',full:'linear-gradient(135deg,#f85149,#da3633)',parallel:'linear-gradient(135deg,#58a6ff,#388bfd)'};modes.forEach(md=>{const btn=$(btnMap[md]);if(btn){btn.style.background=md===m?gradients[md]:'#2a2a4a';btn.style.color=md===m?'#000':'#888';btn.style.border=md===m?'none':'1px solid #3a3a5a'}});const titles={auto:'New task',fast:'Fast mode',standard:'Standard mode',full:'Full mode',parallel:'Parallel mode'};const subs={auto:'task를 분석해서 최적 경로 자동 선택<br>코드 수정, 브레인스토밍, 문서 작성, PPT 생성, 아키텍처 설계 등 모든 작업',fast:'간단한 수정, 저위험 작업<br>빠른 1-pass 처리',standard:'중간 복잡도, pair gen + critic<br>일반적인 개발 작업에 최적',full:'고난도 작업, 풀체인 + aux critic<br>아키텍처 설계, 보안 감사, PPT/PDF 생성',parallel:'비판 없이 2~3 AI 병렬 생성<br>속도 최적화, 파트별 분할 작업'};const roles={auto:'<span style="color:#bc8cff">Classifier</span><span style="color:#58a6ff">Auto Router</span><span style="color:#69db7c">Best Engine</span>',fast:'<span style="color:#3fb950">Claude (1-pass)</span><span style="color:#69db7c">Fast Response</span>',standard:'<span style="color:#d29922">Claude (Gen)</span><span style="color:#ff6b6b">Critics</span><span style="color:#da77f2">Synth</span>',full:'<span style="color:#f85149">Multi-AI (Gen)</span><span style="color:#ff6b6b">Codex+Gemini+Aux (Critics)</span><span style="color:#da77f2">Opus (Synth)</span>',parallel:'<span style="color:#58a6ff">Claude</span><span style="color:#388bfd">Codex</span><span style="color:#69db7c">Gemini (opt)</span>'};$('emptyTitle').textContent=titles[m]||'New task';$('emptySub').innerHTML=subs[m]||'';$('btnRun').textContent='Run';$('autoOpts').style.display=m==='auto'?'flex':'none';$('parallelOpts').style.display=m==='parallel'?'flex':'none';$('fullOpts').style.display=m==='full'?'flex':'none';$('rolesInfo').innerHTML=roles[m]||''}
async function startRun(){const task=$('taskInput').value.trim();if(!task||run)return;run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';lmc=0;
  const body={task,mode:curMode,claude_model:$('modelSelect').value};
  if(curMode==='auto'){const sc=$('autoScope').value;const ri=$('autoRisk').value;const ar=$('autoArtifact').value;if(sc!=='auto')body.scope=sc;if(ri!=='auto')body.risk=ri;if(ar!=='none')body.artifact_type=ar}
  if(curMode==='parallel'){body.pair_mode='pair'+$('parallelParts').value;const od=$('outputDir').value.trim();if(od)body.output_dir=od}
  if(curMode==='full'){const ar=$('fullArtifact').value;if(ar!=='none')body.artifact_type=ar;body.audience=$('fullAudience').value;body.tone=$('fullTone').value}
  const r=await fetch('/api/horcrux/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();
  if(d.solution){run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';$('messages').innerHTML=`<div class="msg msg-generator"><div class="msg-header"><span class="role-tag role-generator">Solution</span><span style="font-size:10px;color:#555">(${d.mode} / ${d.internal_engine})</span></div><pre>${esc(d.solution)}</pre></div>`;$('resultArea').innerHTML=`<div class="result result-ok"><div class="result-header"><span class="result-icon">✅</span><div><div class="result-title" style="color:#69db7c">${d.status}</div><div class="result-sub">${d.mode} · ${d.internal_engine} · score: ${d.score||0}/10</div></div><button class="btn-copy" onclick="navigator.clipboard.writeText(document.querySelector('.msg pre').textContent)">Copy</button></div></div>`;loadThreads();return}
  cid=d.job_id;loadThreads();pt=setInterval(poll,2000)}
async function poll(){if(!cid)return;
  const r=await fetch(statusUrl(cid));if(!r.ok){clearInterval(pt);run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';return}const s=await r.json();
  const isP=(cid||'').startsWith('plan_');const isPair=(cid||'').startsWith('pair_');
  if(isPair){const numParts=s.mode==='pair3'?3:2;const done=s.parts_done||0;$('progressLabel').textContent=`${s.phase||'parallel_gen'} — ${done}/${numParts} parts done`;$('progressFill').style.width=Math.min((done/numParts)*100,100)+'%';$('progressScore').textContent=s.mode==='pair3'?'3-AI Parallel':'2-AI Parallel';$('progressScore').style.color='#69db7c'}else if(isP){const phaseIdx={starting:0,generating:1,synthesizing:2,critiquing:3,polishing:4,completed:4};const pi=phaseIdx[s.phase]||0;$('progressLabel').textContent=`Phase ${pi}/4 — ${s.phase_detail||s.phase||'...'}`;$('progressFill').style.width=Math.min((pi/4)*100,100)+'%';if(s.avg_score>0){$('progressScore').textContent=`Avg Critic: ${s.avg_score.toFixed(1)}/10`;$('progressScore').style.color=s.avg_score>=THRESHOLD?'#69db7c':'#ff6b6b'}}else{$('progressLabel').textContent=`Round ${s.round||0}/${MAX_ROUNDS} - ${s.phase||'...'}`;
  $('progressFill').style.width=Math.min(((s.round||0)/MAX_ROUNDS)*100,100)+"%";
  if(s.avg_score>0){$('progressScore').textContent=`Score: ${s.avg_score.toFixed(1)} / ${THRESHOLD}`;$('progressScore').style.color=s.avg_score>=THRESHOLD?'#69db7c':'#ff6b6b'}}
  if(s.status!=='running'){
    clearInterval(pt);run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';
    const fr=await fetch(resultUrl(cid));const full=await fr.json();
    renderAll(full);renderResult(full);loadThreads();
  } else if(s.message_count>lmc){
    const fr=await fetch(resultUrl(cid));const full=await fr.json();
    const c=$('messages');const msgs=full.messages||[];
    const isP2=(cid||'').startsWith('plan_');for(let i=lmc;i<msgs.length;i++){const m=msgs[i];if(!isP2&&m.role==='generator'){c.innerHTML+=`<div class="round-divider">Round ${Math.floor(i/3)+1}</div>`}if(isP2){const phaseMap={generator:'Phase 1: Generate',synthesizer:'Phase 2: Synthesize',critic:'Phase 3: Critique',final:'Phase 4: Final Polish'};const ph=phaseMap[m.role];if(ph&&!c.innerHTML.includes(ph)){c.innerHTML+=`<div class="round-divider">${ph}</div>`}}const ro=ROLES[m.role]||{name:m.role,cls:'generator'};const label=m.label||ro.name||m.role;const cls=ro.cls;let sh='';if(m.score!==undefined){const p=m.score>=THRESHOLD;sh=`<span class="score ${p?'score-pass':'score-fail'}">${Number(m.score).toFixed(1)}/10</span>`}const modelTag=m.model?` <span style="font-size:10px;color:#555">(${m.model})</span>`:'';c.innerHTML+=`<div class="msg msg-${cls}"><div class="msg-header"><span class="role-tag role-${cls}">${label}</span>${modelTag}${sh}</div><pre>${esc(m.content)}</pre></div>`}
    lmc=msgs.length;sb();
  }}
async function stopRun(){if(!cid)return;const isP=(cid||'').startsWith('plan_');const isPair=(cid||'').startsWith('pair_');const isSi=(cid||'').startsWith('si_');const isHrx=(cid||'').startsWith('hrx_')||(cid||'').startsWith('adp_');if(isP){await fetch(`/api/planning/stop/${cid}`,{method:'POST'})}else if(isPair){await fetch(`/api/pair/stop/${cid}`,{method:'POST'})}else if(isHrx){await fetch(`/api/horcrux/stop/${cid}`,{method:'POST'})}else{await fetch(`/api/stop/${cid}`,{method:'POST'})}}
async function deepDive(parentId){const parentFull=await(await fetch(`/api/result/${parentId}`)).json();const task=parentFull.task||'';const focusHint=prompt(`Deep Dive 포커스 힌트 (선택사항, Enter 스킵):\n현재 task: ${task.slice(0,80)}`)??'';const finalTask=focusHint.trim()?`${task}\n\n[Deep Dive 포커스]: ${focusHint}`:task;if(!finalTask)return;if(pt)clearInterval(pt);run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';lmc=0;$('taskInput').value=finalTask;const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({task:finalTask,threshold:THRESHOLD,max_rounds:MAX_ROUNDS,parent_debate_id:parentId})});const d=await r.json();cid=d.debate_id;loadThreads();pt=setInterval(poll,1500)}
function copyResult(){fetch(resultUrl(cid)).then(r=>r.json()).then(s=>{const isPair=(cid||'').startsWith('pair_');let text='';if(isPair){const parts=s.messages||[];text=parts.filter(m=>m.role!=='architect').map(m=>`// === ${m.role} (${m.model||'unknown'}) ===\n${m.content}`).join('\n\n')}else{text=s.final_solution||s.final_plan||''}navigator.clipboard.writeText(text);const b=document.querySelector('.btn-copy');if(b){b.textContent='Copied!';setTimeout(()=>b.textContent='Copy',1500)}})}
function sb(){$('content').scrollTop=$('content').scrollHeight}
function $(id){return document.getElementById(id)}
function esc(t){return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
setMode('auto');loadThreads();
</script>
</body>
</html>
"""

register_planning_v2_routes(app)


# ── Horcrux v8 Unified API routes ──

horcrux_states = {}

@app.route("/api/horcrux/classify", methods=["POST"])
def horcrux_classify():
    """분류 미리보기 — 실행하지 않고 어떤 모드/엔진이 선택될지 확인."""
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
    if not task:
        return jsonify({"error": "task required"}), 400
    try:
        from core.adaptive import classify_task_complexity, build_stage_plan
        mode_override = data.get("mode", "auto")
        if mode_override == "auto":
            mode_override = None
        result = classify_task_complexity(
            task_description=task,
            task_type=data.get("task_type", "code"),
            num_files_touched=data.get("num_files", 1),
            estimated_scope=data.get("scope", "medium"),
            risk_level=data.get("risk", "medium"),
            artifact_type=data.get("artifact_type", "none"),
            user_mode_override=mode_override,
        )
        d = result.to_dict()
        try:
            plan = build_stage_plan(
                recommended_mode=result.recommended_mode,
                task_type=data.get("task_type", "code"),
                artifact_type=data.get("artifact_type", "none"),
            )
            d["stages"] = plan.enabled_stages
        except Exception:
            d["stages"] = []
        return jsonify(d)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/horcrux/run", methods=["POST"])
def horcrux_run():
    """통합 실행 엔드포인트. classify → engine 결정 → 해당 엔진 호출."""
    data = request.json
    task = _sanitize_task(data.get("task", "").strip())
    if not task:
        return jsonify({"error": "task required"}), 400

    from core.adaptive import classify_task_complexity

    mode_param = data.get("mode", "auto")
    mode_override = None if mode_param == "auto" else mode_param

    claude_model_param = data.get("claude_model", "")

    classification = classify_task_complexity(
        task_description=task,
        task_type=data.get("task_type", "code"),
        num_files_touched=data.get("num_files", 1),
        estimated_scope=data.get("scope", "medium"),
        risk_level=data.get("risk", "medium"),
        artifact_type=data.get("artifact_type", "none"),
        user_mode_override=mode_override,
        claude_model=claude_model_param,
    )

    # Sonnet 보정: hard → full 승격, easy/medium → Opus 추천 경고
    from core.adaptive.classifier import apply_sonnet_compensation
    classification = apply_sonnet_compensation(
        result=classification,
        claude_model=claude_model_param,
        task_description=task,
        estimated_scope=data.get("scope", "medium"),
        risk_level=data.get("risk", "medium"),
    )

    engine = classification.internal_engine.value
    mode = classification.recommended_mode.value
    if mode == "full_horcrux":
        mode = "full"
    intent = classification.detected_intent.value

    # ── 동기 엔진: adaptive_fast / adaptive_standard / adaptive_full ──
    if engine.startswith("adaptive_"):
        try:
            from adaptive_orchestrator import run_adaptive
            # map engine → mode_override for orchestrator
            orch_mode_map = {
                "adaptive_fast": "fast",
                "adaptive_standard": "standard",
                "adaptive_full": "full_horcrux",
            }
            result = run_adaptive(
                task=task,
                mode_override=orch_mode_map.get(engine),
                task_type=data.get("task_type", "code"),
                num_files=data.get("num_files", 1),
                scope=data.get("scope", "medium"),
                risk=data.get("risk", "medium"),
                artifact_type=data.get("artifact_type", "none"),
                interactive=data.get("interactive", "batch"),
            )
            # BUG-2 fix: 동기 응답에도 job_id 생성 → check()로 조회 가능
            sync_id = "hrx_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            solution_text = result.get("final_solution", "")
            horcrux_states[sync_id] = {
                "id": sync_id, "task": task, "status": "completed",
                "phase": "completed",
                "round": result.get("rounds", 1),
                "messages": [
                    {"role": "generator", "content": solution_text,
                     "model": f"Claude ({engine})", "score": result.get("final_score", 0),
                     "ts": datetime.now().isoformat()},
                ] if solution_text else [],
                "avg_score": result.get("final_score", 0),
                "final_solution": solution_text,
                "created_at": datetime.now().isoformat(),
                "finished_at": datetime.now().isoformat(),
            }
            # 로그 파일로도 저장 (서버 재시작 후에도 스레드 유지)
            try:
                log_file = LOG_DIR / f"{sync_id}.json"
                with open(log_file, "w", encoding="utf-8") as f:
                    json.dump(horcrux_states[sync_id], f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return jsonify({
                "status": "converged" if result.get("converged") else "completed",
                "job_id": sync_id,
                "mode": mode,
                "internal_engine": engine,
                "score": result.get("final_score", 0),
                "rounds": result.get("rounds", 0),
                "solution": result.get("final_solution", ""),
                "routing": {
                    "source": classification.routing_source.value,
                    "confidence": classification.confidence,
                    "intent": intent,
                    "reason": classification.reason,
                },
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: planning_pipeline (직접 호출, self-HTTP 제거) ──
    if engine == "planning_pipeline":
        try:
            from planning_v2 import plannings, run_planning_harness
            planning_id = "plan_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:23]
            claude_model_resolved = {"opus": "claude-opus-4-6", "sonnet": "claude-sonnet-4-6"}.get(data.get("claude_model", ""), "")
            task_type = data.get("task_type", "brainstorm")
            artifact_type = data.get("artifact_type", "doc")
            audience = data.get("audience", "general")
            tone = data.get("tone", "professional")
            project_dir = data.get("project_dir", "")
            if project_dir:
                try:
                    validate_project_dir(project_dir)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400

            plannings[planning_id] = {
                "id": planning_id, "task": task, "task_type": task_type,
                "artifact_type": artifact_type, "status": "running",
                "phase": "starting", "phase_detail": "", "messages": [],
                "merged_plan": "", "final_plan": "", "final_solution": "",
                "artifact_spec": None, "avg_score": 0, "error": None,
                "abort": False, "claude_model": claude_model_resolved,
                "audience": audience, "purpose": task[:200], "tone": tone,
                "created_at": datetime.now().isoformat(), "finished_at": None,
            }
            t = threading.Thread(
                target=run_planning_harness,
                args=(planning_id, task, task_type, artifact_type, claude_model_resolved,
                      audience, task[:200], tone, 7.5, 3, project_dir),
                daemon=True,
            )
            t.start()
            return jsonify({
                "status": "running",
                "job_id": planning_id,
                "internal_engine": engine,
                "mode": mode,
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: pair_generation (직접 호출) ──
    if engine == "pair_generation":
        try:
            pair_mode = data.get("pair_mode", "pair2")
            pair_id = "pair_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:23]
            output_dir = data.get("output_dir", "")
            pairs[pair_id] = {
                "id": pair_id, "task": task, "mode": pair_mode,
                "status": "running", "phase": "splitting", "messages": [],
                "results": {}, "spec": "", "error": None,
                "output_dir": output_dir,
                "created_at": datetime.now().isoformat(), "finished_at": None,
            }
            t = threading.Thread(target=run_pair, args=(pair_id, task, pair_mode, "", None), daemon=True)
            t.start()
            return jsonify({
                "status": "running",
                "job_id": pair_id,
                "internal_engine": engine,
                "mode": "parallel",
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: self_improve (직접 호출) ──
    if engine == "self_improve":
        try:
            si_id = "si_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            iterations = data.get("iterations", 3)
            self_improves[si_id] = {
                "id": si_id, "task": task, "status": "running",
                "iteration": 0, "total_iterations": iterations,
                "final_score": 0, "final_solution": "",
                "created_at": datetime.now().isoformat(), "finished_at": None,
            }
            t = threading.Thread(target=run_self_improve, args=(si_id, task, iterations), daemon=True)
            t.start()
            return jsonify({
                "status": "running",
                "job_id": si_id,
                "internal_engine": engine,
                "mode": mode,
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: deep_refactor (직접 호출) ──
    if engine == "deep_refactor":
        try:
            project_dir = data.get("project_dir", "")
            if not project_dir:
                return jsonify({"error": "project_dir is required for deep_refactor mode"}), 400
            try:
                validate_project_dir(project_dir)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            drf_id = "drf_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:23]
            claude_model_resolved = data.get("claude_model", "opus")
            threshold = data.get("threshold", 7.5)
            max_rounds = data.get("max_rounds", 3)
            create_drf_state(drf_id, task, project_dir)
            t = threading.Thread(
                target=run_deep_refactor,
                args=(drf_id, task, project_dir, claude_model_resolved, threshold, max_rounds),
                daemon=True,
            )
            t.start()
            return jsonify({
                "status": "running",
                "job_id": drf_id,
                "internal_engine": engine,
                "mode": "deep_refactor",
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── 비동기 엔진: debate_loop (직접 호출) ──
    if engine == "debate_loop":
        try:
            debate_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            claude_model_resolved = {"opus": "claude-opus-4-6", "sonnet": "claude-sonnet-4-6"}.get(data.get("claude_model", ""), "")
            max_rounds = data.get("max_rounds", 5)
            threshold = data.get("threshold", 8.0)
            project_dir = data.get("project_dir", "")
            if project_dir:
                try:
                    validate_project_dir(project_dir)
                except ValueError as e:
                    return jsonify({"error": str(e)}), 400
            debates[debate_id] = {
                "id": debate_id, "task": task, "status": "running",
                "round": 0, "phase": "starting", "messages": [],
                "avg_score": 0, "final_solution": "",
                "threshold": threshold, "max_rounds": max_rounds,
                "claude_model": claude_model_resolved,
                "project_dir": project_dir,
                "created_at": datetime.now().isoformat(), "finished_at": None,
            }
            vision_url_param = data.get("vision_url", "")
            t = threading.Thread(target=run_debate, args=(debate_id, task, threshold, max_rounds, "", claude_model_resolved, vision_url_param), daemon=True)
            t.start()
            return jsonify({
                "status": "running",
                "job_id": debate_id,
                "internal_engine": engine,
                "mode": mode,
                "message": "Use check(job_id) to monitor",
                "routing": {"source": classification.routing_source.value, "confidence": classification.confidence, "intent": intent},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": f"unknown engine: {engine}"}), 400


@app.route("/api/horcrux/status/<job_id>")
def horcrux_status(job_id):
    """통합 상태 확인 — job_id prefix 기반 라우팅."""
    # deep_refactor 상태는 별도 dict
    state = deep_refactors.get(job_id) if job_id.startswith("drf_") else None
    if not state:
        state = horcrux_states.get(job_id)
    if not state:
        log_file = LOG_DIR / f"{job_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            horcrux_states[job_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": state.get("id"), "status": state.get("status"),
        "phase": state.get("phase", ""), "task": state.get("task", ""),
        "message_count": len(state.get("messages", [])),
        "avg_score": state.get("avg_score", 0),
        "created_at": state.get("created_at"), "finished_at": state.get("finished_at"),
        "error": state.get("error"),
    })


@app.route("/api/horcrux/result/<job_id>")
def horcrux_result(job_id):
    state = deep_refactors.get(job_id) if job_id.startswith("drf_") else None
    if not state:
        state = horcrux_states.get(job_id)
    if not state:
        log_file = LOG_DIR / f"{job_id}.json"
        if log_file.exists():
            with open(log_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            horcrux_states[job_id] = state
        else:
            return jsonify({"error": "not found"}), 404
    return jsonify(state)


@app.route("/api/horcrux/stop/<job_id>", methods=["POST"])
def horcrux_stop(job_id):
    state = horcrux_states.get(job_id)
    if state:
        state["status"] = "aborted"
        state["finished_at"] = datetime.now().isoformat()
    # interactive session stop
    i_sess = interactive_sessions.get(job_id)
    if i_sess:
        from core.adaptive import SessionCommand, FeedbackAction
        i_sess.resume(SessionCommand(action=FeedbackAction.STOP))
    return jsonify({"ok": True})


# ── Interactive Session store ──
interactive_sessions = {}


@app.route("/api/horcrux/feedback", methods=["POST"])
def horcrux_feedback():
    """Interactive session에 피드백 주입 + 다음 라운드 재개."""
    data = request.json
    job_id = data.get("job_id", "")
    action = data.get("action", "continue")

    i_sess = interactive_sessions.get(job_id)
    if not i_sess:
        return jsonify({"error": f"interactive session not found: {job_id}"}), 404

    from core.adaptive import SessionCommand, FeedbackAction as FA

    action_map = {
        "continue": FA.CONTINUE, "feedback": FA.FEEDBACK,
        "focus": FA.FOCUS, "stop": FA.STOP, "rollback": FA.ROLLBACK,
    }
    fa = action_map.get(action, FA.CONTINUE)

    cmd = SessionCommand(
        action=fa,
        human_directive=data.get("human_directive", ""),
        focus_area=data.get("focus_area", ""),
        focus_depth=data.get("focus_depth", "deep"),
        rollback_to_round=data.get("rollback_to_round", 0),
        new_directive=data.get("new_directive", ""),
    )

    # rollback 시 irreversible 경고
    if fa == FA.ROLLBACK and i_sess.side_effects.has_irreversible_after(cmd.rollback_to_round):
        irr = i_sess.side_effects.irreversible_rounds_after(cmd.rollback_to_round)
        return jsonify({
            "status": "warning",
            "irreversible_warning": f"Rounds {irr} have irreversible side effects. Send again to confirm.",
            "irreversible_rounds": irr,
        })

    i_sess.resume(cmd)
    return jsonify({
        "status": i_sess.state.value,
        "message": f"action={action} applied",
        "next_round": i_sess.current_round + 1,
    })


@app.route("/api/horcrux/session/<job_id>")
def horcrux_session(job_id):
    """Interactive session 상세 상태."""
    i_sess = interactive_sessions.get(job_id)
    if not i_sess:
        return jsonify({"error": "not found"}), 404
    return jsonify(i_sess.to_dict())


# ── Analytics API routes (Phase 3) ──

@app.route("/api/analytics")
def analytics_dashboard():
    try:
        from core.adaptive.analytics import build_analytics_dashboard
        dashboard = build_analytics_dashboard()
        return jsonify(dashboard.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics/timeouts")
def analytics_timeouts():
    try:
        from core.adaptive.analytics import compute_latency_percentiles, auto_tune_timeouts
        stats = {k: v.to_dict() for k, v in compute_latency_percentiles().items()}
        recommendations = auto_tune_timeouts(dry_run=True)
        return jsonify({"stats": stats, "recommendations": recommendations})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics/timeouts/apply", methods=["POST"])
def analytics_apply_timeouts():
    try:
        from core.adaptive.analytics import auto_tune_timeouts
        applied = auto_tune_timeouts(dry_run=False)
        return jsonify({"applied": applied, "status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics/critics")
def analytics_critics():
    try:
        from core.adaptive.analytics import compute_critic_reliability
        data = {k: v.to_dict() for k, v in compute_critic_reliability().items()}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics/modes")
def analytics_modes():
    try:
        from core.adaptive.analytics import compute_mode_usage_stats
        data = {k: v.to_dict() for k, v in compute_mode_usage_stats().items()}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics/heuristic")
def analytics_heuristic():
    try:
        from core.adaptive.analytics import suggest_heuristic_refinements
        data = suggest_heuristic_refinements()
        return jsonify(data.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics/scoring")
def analytics_scoring():
    """현재 scoring 가중치 조회 (dry_run)."""
    try:
        from core.adaptive.analytics import auto_tune_scoring_weights
        return jsonify(auto_tune_scoring_weights(dry_run=True))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics/scoring/apply", methods=["POST"])
def analytics_scoring_apply():
    """scoring 가중치 자동 튜닝 → config.json 저장."""
    try:
        from core.adaptive.analytics import auto_tune_scoring_weights
        result = auto_tune_scoring_weights(dry_run=False)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Deep Refactor 의존성 주입
    inject_drf_callers(
        call_claude=call_claude, call_codex=call_codex, call_gemini=call_gemini,
        call_aux_critic_fn=_call_aux_critic, aux_endpoints=AUX_CRITIC_ENDPOINTS,
        extract_json_fn=extract_json, extract_score_fn=extract_score,
        log_dir=str(LOG_DIR),
    )

    print("\nHorcrux v8 - Adaptive Single Entry Point")
    print("  External: Auto / Fast / Standard / Full / Parallel / Deep Refactor")
    print("  Internal: adaptive_fast/standard/full, debate_loop, planning_pipeline, pair_generation, self_improve, deep_refactor")
    print("  Unified endpoint: /api/horcrux/run → classify → auto-route")
    print()
    # Aux API 키 감지 로그
    for name, _, env_key, model, _ in AUX_CRITIC_ENDPOINTS:
        val = os.environ.get(env_key, "")
        if val:
            print(f"  [AUX] {name} ({model}): KEY SET ({env_key}={val[:8]}...)")
        else:
            print(f"  [AUX] {name}: KEY MISSING ({env_key})")
    print(f"\n  http://localhost:5000")
    print(f"  Modes: Auto | Fast | Standard | Full | Parallel\n")
    # R06: localhost 전용 바인딩 (외부 접근 차단)
    _host = os.environ.get("HORCRUX_HOST", "127.0.0.1")
    app.run(host=_host, port=5000, debug=False)
