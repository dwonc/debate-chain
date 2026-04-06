"""
Horcrux — Planning Pipeline v2 (Layer 3)
Content Profile + Artifact Profile 통합 하네스

routing:
  brainstorm  → content_profile only
  portfolio   → content_profile only
  hybrid      → content_profile → artifact_profile
  artifact_only → artifact_profile

content_profile pipeline:
  generator(3 models) → synthesizer → critic → convergence_check → revision loop → finalize

artifact_profile pipeline:
  artifact_spec_builder → artifact_critic → artifact_revision → artifact_renderer
"""

import json
import os
import re
import time
import threading
import concurrent.futures
from datetime import datetime
from pathlib import Path

# ═══════════════════════════════════════════
# P2-001: Direct imports (global injection 제거)
# ═══════════════════════════════════════════
from core.llm import (
    call_claude as _call_claude,
    call_codex as _call_codex,
    call_gemini as _call_gemini,
    _call_aux_critic,
    AUX_CRITIC_ENDPOINTS as _AUX_CRITIC_ENDPOINTS,
)
from core.engine.critic import (
    extract_json as _extract_json,
    extract_score as _extract_score,
    normalize_critic_output as _normalize_critic,
    check_convergence_v2 as _check_convergence_v2,
    build_revision_focus as _build_revision_focus,
    build_compact_context_package as _build_compact_context,
    format_issues_compact as _format_issues_compact,
)
_LOG_DIR = str(Path(__file__).parent / "logs")


def inject_callers(**kwargs):
    """P2-001: 하위 호환용 no-op. 직접 import로 대체됨."""
    pass


# ═══════════════════════════════════════════
# PROFILE CONFIG — task_type별 행동 분기
# ═══════════════════════════════════════════

PROFILE_CONFIG = {
    "brainstorm": {
        "generator_instruction": "Explore WIDELY. Maximize diversity and novelty. Propose unconventional ideas. Do NOT self-censor. Quantity over polish.",
        "generator_roles": {
            "claude": "creative strategist who thinks outside the box",
            "codex": "technical innovator exploring bleeding-edge approaches",
            "gemini": "user advocate finding unmet needs and hidden opportunities",
        },
        "critic_dimensions": ["novelty", "diversity", "feasibility", "blind_spots"],
        "critic_score_schema": '{{"scores":{{"novelty":<1-10>,"diversity":<1-10>,"feasibility":<1-10>,"blind_spots":<1-10>}},"overall":<1-10>,"summary":"<2 sentences>","issues":[{{"id":"ISS-<NNN>","sev":"critical|major|minor","desc":"<issue>","fix":"<suggestion>","dimension":"novelty|diversity|feasibility|blind_spots"}}],"resolved_from_previous":[{{"id":"ISS-<NNN>","status":"resolved|unresolved"}}],"regressions":["<regressed issue if any>"],"strengths":["s1"]}}',
        "critic_roles": {
            "codex": "idea diversity and technical feasibility critic",
            "gemini": "novelty and blind spot detection critic",
        },
        "aux_critic_role": "idea diversity and originality reviewer",
        "revision_strength": "light",  # 아이디어 죽이지 않음
        "polish_instruction": "Light polish only: group similar ideas, improve readability, add brief labels. Do NOT delete any ideas or make them generic.",
        "convergence_threshold_offset": -0.5,  # brainstorm은 수렴 기준 낮춤
    },
    "portfolio": {
        "generator_instruction": "Produce STRUCTURED, evidence-based content. Focus on clarity, persuasiveness, and audience fit. Include concrete examples and metrics.",
        "generator_roles": {
            "claude": "senior portfolio consultant and storyteller",
            "codex": "technical writer who explains complex systems clearly",
            "gemini": "audience-focused content strategist and editor",
        },
        "critic_dimensions": ["clarity", "structure", "persuasiveness", "completeness"],
        "critic_score_schema": '{{"scores":{{"clarity":<1-10>,"structure":<1-10>,"persuasiveness":<1-10>,"completeness":<1-10>}},"overall":<1-10>,"summary":"<2 sentences>","issues":[{{"id":"ISS-<NNN>","sev":"critical|major|minor","desc":"<issue>","fix":"<suggestion>","dimension":"clarity|structure|persuasiveness|completeness"}}],"resolved_from_previous":[{{"id":"ISS-<NNN>","status":"resolved|unresolved"}}],"regressions":["<regressed issue if any>"],"strengths":["s1"]}}',
        "critic_roles": {
            "codex": "structure and completeness critic",
            "gemini": "clarity, persuasiveness, and audience-fit critic",
        },
        "aux_critic_role": "document quality and readability reviewer",
        "revision_strength": "strong",  # 누락/설득력/흐름 강하게 수정
        "polish_instruction": "Full polish: improve sentence quality, fix flow, ensure tone consistency, remove redundancy, strengthen transitions. Do NOT change core strategy or add unsupported claims.",
        "convergence_threshold_offset": 0.0,
    },
    "hybrid": {
        "generator_instruction": "Start broad with diverse ideas, then converge toward structured, actionable content. Balance creativity with clarity.",
        "generator_roles": {
            "claude": "business strategist and system architect",
            "codex": "senior developer and technical lead",
            "gemini": "product manager and user advocate",
        },
        "critic_dimensions": ["correctness", "completeness", "coherence", "actionability"],
        "critic_score_schema": '{{"scores":{{"correctness":<1-10>,"completeness":<1-10>,"coherence":<1-10>,"actionability":<1-10>}},"overall":<1-10>,"summary":"<2 sentences>","issues":[{{"id":"ISS-<NNN>","sev":"critical|major|minor","desc":"<issue>","fix":"<suggestion>","dimension":"correctness|completeness|coherence|actionability"}}],"resolved_from_previous":[{{"id":"ISS-<NNN>","status":"resolved|unresolved"}}],"regressions":["<regressed issue if any>"],"strengths":["s1"]}}',
        "critic_roles": {
            "codex": "technical feasibility and implementation quality critic",
            "gemini": "user experience, clarity, and market fit critic",
        },
        "aux_critic_role": "independent reviewer with fresh eyes",
        "revision_strength": "moderate",
        "polish_instruction": "Moderate polish: ensure document quality and flow while preserving creative insights. Fix structural issues and strengthen weak sections.",
        "convergence_threshold_offset": 0.0,
    },
}
# artifact_only는 content_profile을 안 쓰므로 hybrid fallback
PROFILE_CONFIG["artifact_only"] = PROFILE_CONFIG["hybrid"]


def _get_profile(task_type: str) -> dict:
    """task_type에 맞는 profile config 반환. 없으면 hybrid fallback."""
    return PROFILE_CONFIG.get(task_type, PROFILE_CONFIG["hybrid"])


def _detect_language_instruction(task: str) -> str:
    """task 언어를 감지해서 출력 언어 지시문 반환."""
    # 한글 유니코드 범위: AC00-D7AF (완성형), 3131-318E (자모)
    korean_chars = sum(1 for c in task if '\uAC00' <= c <= '\uD7AF' or '\u3131' <= c <= '\u318E')
    ratio = korean_chars / max(len(task), 1)
    if ratio > 0.05:  # 한글이 5% 이상이면 한국어 task
        return "IMPORTANT: 반드시 한국어로 답변하라. All output MUST be in Korean."
    return ""


# ═══════════════════════════════════════════
# PROMPTS — Content Profile (profile-aware)
# ═══════════════════════════════════════════

CONTENT_GENERATOR_PROMPT = """You are a {role} expert.

CRITICAL: You are a text generator, not an interactive agent. Do NOT request file access, permissions, tool calls, directory listings, or any external actions. Do NOT mention working directories, session limitations, or access restrictions. Generate the requested content directly based on the task description and available information. If you lack specific implementation details, make reasonable assumptions and clearly mark them as [ASSUMPTION].

## Task
{task}

## Your Approach
{profile_instruction}

CRITICAL: Read the task carefully. If the task specifies a desired output format, produce output in EXACTLY that format.

If the task does NOT specify an output format, reply JSON only:
{{"content":"<detailed output>","approach":"<1 sentence>","decisions":[{{"topic":"<what>","choice":"<chosen>","reason":"<why>"}}],"rejected_alternatives":["considered but rejected approach 1","considered but rejected approach 2"],"key_messages":["core point 1","core point 2"]}}

Write in the same language as the task. Be thorough and detailed.
IMPORTANT: Complete ALL requested items. If asked for N items, output ALL N items. Do NOT truncate, abbreviate, or stop early. If output is long, that is fine — completeness is mandatory."""

SYNTH_PROMPT_V2 = """You are a master synthesizer. Three experts independently produced outputs for the same task.
Synthesize the BEST parts of all three into a single, superior result.

CRITICAL: You are a text generator, not an interactive agent. Do NOT request file access, permissions, tool calls, or any external actions. Do NOT mention working directories or session limitations. Generate content directly.

Task:
{task}

=== Expert A (Strategic/Business) ===
{plan_a}

=== Expert B (Technical/Feasibility) ===
{plan_b}

=== Expert C (User/Market) ===
{plan_c}

Instructions:
1. Identify the strongest elements from each expert
2. Resolve contradictions — pick the better-reasoned position
3. Merge into a unified result that surpasses any individual one
4. Remove duplicates and weak points
5. Preserve all strong, unique insights from each expert

Reply JSON only:
{{"content":"<synthesized complete output>","structure_summary":"<how the output is organized>","key_messages":["msg1","msg2","msg3"],"source_decisions":[{{"topic":"<what>","choice":"<chosen>","reason":"<why>","from":"A|B|C|merged"}}],"strengths_preserved":["from A: ...","from B: ...","from C: ..."]}}

Write in the same language as the task. Be thorough — do NOT truncate or abbreviate. Complete ALL sections fully."""

CONTENT_CRITIC_PROMPT = """You are a {role} critic. Critically evaluate this output.

CRITICAL: You are a text evaluator, not an interactive agent. Do NOT request file access, permissions, tool calls, or any external actions. Evaluate based on the content provided below.

Task:
{task}

Output to evaluate:
{content}

Previously fixed issues (check for regressions):
{previously_fixed}

Analyze from your {role} perspective. Score each dimension 1-10.
Reply JSON only:
{score_schema}"""

CONTENT_IMPROVE_PROMPT = """CRITICAL: You are a text improver, not an interactive agent. Do NOT request file access, permissions, tool calls, or any external actions. Do NOT mention working directories or session limitations. Improve the content directly based on the issues listed below. If you lack specific details, make reasonable assumptions marked as [ASSUMPTION].

Task: {task}

Current content:
{content}

## Blocking Issues (fix these FIRST)
{blocking_issues}

## Regressions (must eliminate)
{regressions}

## Worst Dimensions
{worst_dimensions}

## Critic Disagreements
{critic_disagreements}

## Alternative Approaches
{alternative_views}

## PRESERVE (do NOT change these)
{preserve}

## Previously fixed issues (do NOT regress)
{previously_fixed}

Fix blocking issues first. Do NOT rewrite passing areas.
Reply JSON only:
{{"content":"<improved complete output>","approach":"<1 sentence>","changes":["fix1","fix2"],"rejected_alternatives":["alt considered but not used"],"key_messages":["preserved or updated messages"]}}"""

CONTENT_POLISH_PROMPT = """CRITICAL: You are a text polisher, not an interactive agent. Do NOT request file access, permissions, tool calls, or any external actions. Polish the content directly.

Task: {task}

Content to polish:
{content}

## Polish Instructions
{polish_instruction}

## Key Messages (MUST preserve)
{key_messages}

Apply the polish instructions above. Output the polished version.
Reply JSON only:
{{"content":"<polished complete output>","changes":["polish1","polish2"],"key_messages":["preserved messages"]}}"""


# ═══════════════════════════════════════════
# PROMPTS — Artifact Profile
# ═══════════════════════════════════════════

ARTIFACT_SPEC_BUILDER_PROMPT = """You are a document architect. Convert finalized content into a structured artifact specification.

CRITICAL: You are a text generator, not an interactive agent. Do NOT request file access, permissions, tool calls, or any external actions. Generate the spec directly.

Task: {task}
Artifact type: {artifact_type}
Target audience: {audience}
Purpose: {purpose}
Tone: {tone}

## Finalized Content
{final_content}

## Key Messages (MUST appear in artifact)
{key_messages}

## Must NOT Change
{must_not_change}

## Source Decisions
{source_decisions}

Convert the content into a {artifact_type} specification.

{format_instructions}

CRITICAL RULES:
- Do NOT add new claims, arguments, or data not present in the content
- Do NOT remove or weaken any key message
- Preserve all must_not_change items exactly
- Every section/slide must trace back to the source content
- Reply JSON only"""

ARTIFACT_CRITIC_PROMPT = """CRITICAL: You are a text evaluator, not an interactive agent. Do NOT request file access, permissions, tool calls, or any external actions. Evaluate based on the content provided below.

You are an artifact quality inspector. Check this {artifact_type} spec for:
1. Information completeness — is anything from the source content missing?
2. Flow/logic — do sections/slides follow a logical progression?
3. Key message preservation — are all key messages present and prominent?
4. must_not_change compliance — are protected items unchanged?
5. No content drift — nothing added that wasn't in the source

Source content summary:
{content_summary}

Key messages that MUST appear:
{key_messages}

Items that MUST NOT change:
{must_not_change}

Artifact spec to evaluate:
{artifact_spec}

Reply JSON only:
{{"overall":<1-10>,"scores":{{"completeness":<1-10>,"flow":<1-10>,"message_preservation":<1-10>,"no_drift":<1-10>}},"missing_content":["what's missing from source"],"drift_detected":["content added that wasn't in source"],"flow_issues":["where logic breaks"],"must_not_change_violations":["which protected items were changed"],"suggestions":["improvement1"]}}"""

ARTIFACT_RENDERER_PROMPT = """CRITICAL: You are a text renderer, not an interactive agent. Do NOT request file access, permissions, tool calls, or any external actions. Render the content directly.

You are a {artifact_type} renderer. Your ONLY job is to format the spec into final output.

STRICT RULES:
- Do NOT change any content, message, or data
- Do NOT add new arguments, claims, or information
- Do NOT summarize or shorten — preserve everything
- Do NOT drift from the spec
- Your role is LAYOUT and FORMATTING only

Artifact spec:
{artifact_spec}

Render into a complete, ready-to-use {artifact_type}.
{render_instructions}"""


# ═══════════════════════════════════════════
# FORMAT INSTRUCTIONS (artifact_type별)
# ═══════════════════════════════════════════

PPT_FORMAT_INSTRUCTIONS = """Reply with a PPT specification JSON:
{{"deck_title":"<title>","audience":"<target>","slides":[{{"slide_no":<n>,"type":"title|problem|solution|architecture|feature|result|closing","title":"<slide title>","bullets":["point1","point2"],"visual_hint":"<suggested visual>","speaker_note":"<what to say>","must_preserve_points":["key point that must not be removed"]}}]}}"""

DOC_FORMAT_INSTRUCTIONS = """Reply with a Document specification JSON:
{{"document_title":"<title>","sections":[{{"section_no":<n>,"heading":"<section heading>","purpose":"<why this section>","content":"<full section content>","key_points":["point1","point2"],"must_preserve_points":["key point that must not be removed"]}}]}}"""

PDF_FORMAT_INSTRUCTIONS = DOC_FORMAT_INSTRUCTIONS  # PDF uses same structure as doc

README_FORMAT_INSTRUCTIONS = """Reply with a README specification JSON:
{{"document_title":"<title>","sections":[{{"section_no":<n>,"heading":"<section heading>","purpose":"<why this section>","content":"<full markdown content>","key_points":["point1"]}}]}}"""

_ARTIFACT_FORMAT_MAP = {
    "ppt": PPT_FORMAT_INSTRUCTIONS,
    "pdf": PDF_FORMAT_INSTRUCTIONS,
    "doc": DOC_FORMAT_INSTRUCTIONS,
    "readme": README_FORMAT_INSTRUCTIONS,
}

PPT_RENDER_INSTRUCTIONS = """Output the final presentation as structured JSON with complete slide content:
{{"title":"<deck title>","slides":[{{"slide_no":<n>,"title":"<title>","content":"<full content with bullets>","speaker_note":"<note>"}}]}}"""

DOC_RENDER_INSTRUCTIONS = """Output the final document as structured markdown. Include all sections, headings, and content in order."""

_RENDER_INSTRUCTIONS_MAP = {
    "ppt": PPT_RENDER_INSTRUCTIONS,
    "pdf": DOC_RENDER_INSTRUCTIONS,
    "doc": DOC_RENDER_INSTRUCTIONS,
    "readme": DOC_RENDER_INSTRUCTIONS,
}


# ═══════════════════════════════════════════
# PHASE TIMING — 경과시간 + ETA 예측
# ═══════════════════════════════════════════

# phase 순서 (콘텐츠 파이프라인 기준)
CONTENT_PHASES = [
    "content:generating",
    "content:synthesizing",
    "content:critic_r1",
    "content:revising_r1",
    "content:critic_r2",
    "content:revising_r2",
    "content:critic_r3",
    "content:polishing",
    "content:finalizing",
]

# 기본 fallback 예상 시간(초) — 로그가 없을 때 사용
DEFAULT_PHASE_DURATIONS = {
    "content:generating": 120,
    "content:synthesizing": 300,
    "content:critic_r1": 180,
    "content:revising_r1": 180,
    "content:critic_r2": 180,
    "content:revising_r2": 180,
    "content:critic_r3": 180,
    "content:polishing": 180,
    "content:finalizing": 5,
}


def _compute_phase_averages_from_logs() -> dict:
    """과거 planning 로그에서 phase별 평균 소요시간(초) 계산.
    
    각 로그의 messages[].ts 타임스탬프를 분석해서
    role별 소요시간을 측정한다.
    """
    if not _LOG_DIR or not _LOG_DIR.exists():
        return dict(DEFAULT_PHASE_DURATIONS)

    phase_samples = {}  # phase_key -> [durations]
    
    for log_file in sorted(_LOG_DIR.glob("plan_*.json"))[-10:]:  # 최근 10개만
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                log = json.load(f)
            if log.get("status") != "completed":
                continue
            msgs = log.get("messages", [])
            if len(msgs) < 4:
                continue
            
            # 첫 ts ~ 마지막 ts 사이의 role별 구간 측정
            role_map = {
                "generator": "content:generating",
                "synthesizer": "content:synthesizing",
                "critic": "content:critic_r1",
                "diagnostics": "content:critic_r1",  # critic과 묶음
                "revision": "content:revising_r1",
                "polish": "content:polishing",
                "final": "content:finalizing",
            }
            
            # role별 첫/마지막 타임스탬프 수집
            role_times = {}  # role -> (first_ts, last_ts)
            for m in msgs:
                role = m.get("role", "")
                ts_str = m.get("ts", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str)
                except (ValueError, TypeError):
                    continue
                if role not in role_times:
                    role_times[role] = [ts, ts]
                else:
                    role_times[role][1] = ts  # 마지막 업데이트
            
            # 순서대로 구간 계산
            ordered_roles = ["generator", "synthesizer", "critic", "revision", "polish", "final"]
            prev_end = None
            for role in ordered_roles:
                if role not in role_times:
                    continue
                start, end = role_times[role]
                phase_key = role_map.get(role, role)
                if prev_end:
                    duration = (end - prev_end).total_seconds()
                else:
                    duration = (end - start).total_seconds()
                if duration > 0:
                    phase_samples.setdefault(phase_key, []).append(duration)
                prev_end = end
                
        except Exception:
            continue
    
    # 평균 계산
    result = dict(DEFAULT_PHASE_DURATIONS)
    for key, samples in phase_samples.items():
        if samples:
            result[key] = round(sum(samples) / len(samples))
    
    return result


def _estimate_remaining(state: dict, phase_avgs: dict) -> dict:
    """현재 state에서 경과시간과 ETA 계산.
    
    Returns: {
        "elapsed_sec": 총 경과시간,
        "phase_elapsed_sec": 현재 phase 경과시간,
        "eta_total_sec": 예상 총 소요시간,
        "eta_remaining_sec": 남은 예상 시간,
        "progress_pct": 0~100,
    }
    """
    now = time.time()
    started = state.get("_started_epoch", now)
    phase_started = state.get("_phase_started_epoch", now)
    
    elapsed = now - started
    phase_elapsed = now - phase_started
    
    # 완료된 phase 듀레이션 합산
    done_durations = sum(state.get("_phase_durations", {}).values())
    
    # 남은 phase 예상시간 계산
    current_phase = state.get("phase", "")
    done_phases = set(state.get("_phase_durations", {}).keys())
    
    remaining_est = 0
    found_current = False
    for p in CONTENT_PHASES:
        if p in done_phases:
            continue
        # critic_r2 -> critic_r1로 fallback
        p_base = p.split("_r")[0] + "_r1" if "_r" in p else p
        est = phase_avgs.get(p, phase_avgs.get(p_base, 120))
        if not found_current:
            if p == current_phase or current_phase.startswith(p.split("_r")[0]):
                found_current = True
                remaining_est += max(0, est - phase_elapsed)
                continue
        if found_current:
            remaining_est += est
    
    eta_total = done_durations + phase_elapsed + remaining_est
    progress = min(99, int((elapsed / max(eta_total, 1)) * 100)) if eta_total > 0 else 0
    
    return {
        "elapsed_sec": round(elapsed),
        "phase_elapsed_sec": round(phase_elapsed),
        "eta_total_sec": round(eta_total),
        "eta_remaining_sec": round(max(0, remaining_est)),
        "progress_pct": progress,
    }


def _record_phase_transition(state: dict, new_phase: str):
    """현재 phase 종료 시간 기록 후 새 phase 시작."""
    now = time.time()
    old_phase = state.get("phase", "")
    if old_phase and old_phase != new_phase:
        phase_started = state.get("_phase_started_epoch", now)
        duration = now - phase_started
        if "_phase_durations" not in state:
            state["_phase_durations"] = {}
        state["_phase_durations"][old_phase] = round(duration)
    state["_phase_started_epoch"] = now
    state["phase"] = new_phase


# ═══════════════════════════════════════════
# PLANNING STATE
# ═══════════════════════════════════════════
plannings = {}  # planning_id → state


# ═══════════════════════════════════════════
# CONTENT FINALIZE PACKAGE
# ═══════════════════════════════════════════

def build_content_finalize_package(
    final_content: str,
    synth_data: dict,
    task: str,
    artifact_goal: str = "doc",
    audience: str = "general",
    purpose: str = "",
    tone: str = "professional",
) -> dict:
    """확정된 content를 artifact_profile에 넘길 패키지로 빌드.
    
    handoff spec의 content_finalize_package_schema 준수.
    """
    key_messages = []
    preserve = []
    source_decisions = []

    if synth_data and isinstance(synth_data, dict):
        key_messages = synth_data.get("key_messages", [])
        preserve = synth_data.get("strengths_preserved", [])
        source_decisions = synth_data.get("source_decisions", [])

    return {
        "final_content": final_content,
        "structure_summary": synth_data.get("structure_summary", "") if synth_data else "",
        "key_messages": key_messages[:10],
        "preserve": preserve[:10],
        "must_not_change": key_messages[:5],  # top key messages는 변경 불가
        "audience": audience,
        "purpose": purpose or task[:200],
        "tone": tone,
        "artifact_goal": artifact_goal,
        "source_decisions": source_decisions[:10],
    }


# ═══════════════════════════════════════════
# ARTIFACT SPEC BUILDER
# ═══════════════════════════════════════════

def build_artifact_spec(content_package: dict, claude_model: str = "") -> dict:
    """content_finalize_package → artifact JSON spec 변환.
    
    Claude에게 ARTIFACT_SPEC_BUILDER_PROMPT를 보내서 구조화된 spec 생성.
    """
    artifact_type = content_package.get("artifact_goal", "doc")
    format_instr = _ARTIFACT_FORMAT_MAP.get(artifact_type, DOC_FORMAT_INSTRUCTIONS)

    prompt = ARTIFACT_SPEC_BUILDER_PROMPT.format(
        task=content_package.get("purpose", ""),
        artifact_type=artifact_type,
        audience=content_package.get("audience", "general"),
        purpose=content_package.get("purpose", ""),
        tone=content_package.get("tone", "professional"),
        final_content=content_package.get("final_content", "")[:15000],
        key_messages=json.dumps(content_package.get("key_messages", []), ensure_ascii=False),
        must_not_change=json.dumps(content_package.get("must_not_change", []), ensure_ascii=False),
        source_decisions=json.dumps(content_package.get("source_decisions", [])[:5], ensure_ascii=False),
        format_instructions=format_instr,
    )

    raw = _call_claude(prompt, 900, claude_model)
    spec = _extract_json(raw)
    return spec or {"error": "spec_build_failed", "raw": raw[:2000]}


def validate_artifact_spec(spec: dict, content_package: dict) -> dict:
    """artifact spec이 content_package의 preserve/must_not_change를 위반하지 않는지 검사.
    
    Returns: {"valid": bool, "violations": [...], "warnings": [...]}
    """
    violations = []
    warnings = []

    if not spec or "error" in spec:
        return {"valid": False, "violations": ["spec build failed"], "warnings": []}

    # must_not_change 검사: key_messages가 spec 어딘가에 존재하는지
    spec_text = json.dumps(spec, ensure_ascii=False).lower()
    for msg in content_package.get("must_not_change", []):
        if not msg:
            continue
        # 핵심 단어 기반 매칭 (정확한 문자열 대신 주요 키워드 3개 이상)
        words = [w for w in msg.lower().split() if len(w) > 3][:5]
        found = sum(1 for w in words if w in spec_text)
        if len(words) > 0 and found < len(words) * 0.5:
            violations.append(f"Key message possibly missing: '{msg[:80]}'")

    # key_messages 검사
    for msg in content_package.get("key_messages", []):
        if not msg:
            continue
        words = [w for w in msg.lower().split() if len(w) > 3][:5]
        found = sum(1 for w in words if w in spec_text)
        if len(words) > 0 and found < len(words) * 0.3:
            warnings.append(f"Key message may be underrepresented: '{msg[:80]}'")

    # 빈 섹션 검사 (ppt: slides, doc: sections)
    slides = spec.get("slides", spec.get("sections", []))
    if not slides:
        violations.append("No slides/sections in spec")
    for item in slides:
        if isinstance(item, dict):
            content = item.get("content", item.get("bullets", ""))
            if not content:
                warnings.append(f"Empty content in {item.get('title', 'unknown')}")

    return {
        "valid": len(violations) == 0,
        "violations": violations,
        "warnings": warnings,
    }


def render_artifact(spec: dict, artifact_type: str, claude_model: str = "") -> str:
    """artifact spec → 최종 렌더링된 출력물."""
    render_instr = _RENDER_INSTRUCTIONS_MAP.get(artifact_type, DOC_RENDER_INSTRUCTIONS)

    prompt = ARTIFACT_RENDERER_PROMPT.format(
        artifact_type=artifact_type,
        artifact_spec=json.dumps(spec, indent=2, ensure_ascii=False)[:20000],
        render_instructions=render_instr,
    )

    raw = _call_codex(prompt, 900)
    return raw


# ═══════════════════════════════════════════
# CONTENT PROFILE — Multi-model Generate + Converge Loop
# ═══════════════════════════════════════════

def _run_content_generators(task: str, claude_model: str, state: dict,
                            task_type: str = "hybrid") -> dict:
    """Phase 1: 3 models generate content in parallel. profile-aware roles & instruction."""
    GEN_TIMEOUT = 900
    profile = _get_profile(task_type)
    roles = profile["generator_roles"]
    instruction = profile["generator_instruction"]
    ctx_prefix = state.get("_project_context_prefix", "")
    lang_inst = _detect_language_instruction(task)
    prompts = {name: ctx_prefix + (lang_inst + "\n\n" if lang_inst else "") + CONTENT_GENERATOR_PROMPT.format(
                   task=task, role=role, profile_instruction=instruction)
               for name, role in roles.items()}

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        future_map = {
            pool.submit(_call_claude, prompts["claude"], GEN_TIMEOUT, claude_model): "claude",
            pool.submit(_call_codex, prompts["codex"], GEN_TIMEOUT): "codex",
            pool.submit(_call_gemini, prompts["gemini"], GEN_TIMEOUT): "gemini",
        }
        for future in concurrent.futures.as_completed(future_map):
            if state.get("abort"):
                break
            name = future_map[future]
            raw = future.result()
            parsed = _extract_json(raw)
            results[name] = {
                "raw": raw,
                "parsed": parsed,
                "display": json.dumps(parsed, indent=2, ensure_ascii=False) if parsed else raw,
            }
            state["phase_detail"] = f"{len(results)}/3 generators done ({name})"
            model_labels = {"claude": "Generator (Claude)", "codex": "Generator (Codex)", "gemini": "Generator (Gemini)"}
            state["messages"].append({
                "role": "generator", "model": name,
                "label": model_labels.get(name, f"Generator ({name})"),
                "content": results[name]["display"][:15000],
                "ts": datetime.now().isoformat(),
            })

    return results


def _run_content_synthesizer(task: str, gen_results: dict, claude_model: str, state: dict) -> tuple:
    """Phase 2: Synthesize 3 outputs into 1."""
    ctx_prefix = state.get("_project_context_prefix", "")
    lang_inst = _detect_language_instruction(task)
    prompt = ctx_prefix + (lang_inst + "\n\n" if lang_inst else "") + SYNTH_PROMPT_V2.format(
        task=task,
        plan_a=gen_results.get("claude", {}).get("display", "N/A"),
        plan_b=gen_results.get("codex", {}).get("display", "N/A"),
        plan_c=gen_results.get("gemini", {}).get("display", "N/A"),
    )
    raw = _call_claude(prompt, 900, claude_model)
    parsed = _extract_json(raw)
    display = json.dumps(parsed, indent=2, ensure_ascii=False) if parsed else raw

    state["messages"].append({
        "role": "synthesizer", "model": "Claude",
        "label": "Synthesizer (Claude)",
        "content": display[:15000],
        "ts": datetime.now().isoformat(),
    })

    # content 추출
    content = ""
    if parsed:
        content = parsed.get("content", display)
    else:
        content = raw

    return content, parsed


def _run_content_multi_critic(task: str, content: str, previously_fixed: str,
                              state: dict, task_type: str = "hybrid",
                              prev_core_avg: float = 0.0) -> dict:
    """Content profile용 multi-critic: Core(Codex+Gemini) + Aux 병렬. profile-aware 차원/역할.
    P4: prev_core_avg >= 8.0이면 Aux skip (이미 수렴 가까우면 Aux 불필요)."""
    profile = _get_profile(task_type)
    critic_roles = profile["critic_roles"]
    score_schema = profile["critic_score_schema"]
    aux_role = profile["aux_critic_role"]

    results_by_model = {}
    all_issues = []
    all_scores = {}
    seen = set()

    # P4 simplified: core score 평균 >= 8.0이면 aux skip
    import sys
    if prev_core_avg >= 8.0:
        available_aux = []
        sys.stderr.write(f"  [P4] Aux SKIPPED — prev_core_avg={prev_core_avg:.1f} >= 8.0\n")
        sys.stderr.flush()
    else:
        available_aux = [
            ep for ep in (_AUX_CRITIC_ENDPOINTS or [])
            if os.environ.get(ep[2])
        ] if _AUX_CRITIC_ENDPOINTS and _call_aux_critic else []

    # BUG-3 debug: aux critic 가용성 로깅
    sys.stderr.write(f"  [CRITIC] AUX_ENDPOINTS={'set' if _AUX_CRITIC_ENDPOINTS else 'None'}, call_aux={'set' if _call_aux_critic else 'None'}, available={len(available_aux)}\n")
    for ep in available_aux:
        sys.stderr.write(f"    → {ep[0]} ({ep[3]}): key={'SET' if os.environ.get(ep[2]) else 'MISSING'}\n")
    sys.stderr.flush()

    total_workers = 2 + len(available_aux)

    lang_inst = _detect_language_instruction(task)
    _lang_prefix = (lang_inst + "\n\n") if lang_inst else ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(total_workers, 2)) as pool:
        # Core critics (profile-aware roles + score schema)
        futures = {}
        for model_name, role in critic_roles.items():
            prompt = _lang_prefix + CONTENT_CRITIC_PROMPT.format(
                task=task, content=content[:15000], role=role,
                previously_fixed=previously_fixed or "None (first round)",
                score_schema=score_schema,
            )
            if model_name == "codex":
                f = pool.submit(_call_codex, prompt, 600)
            else:
                f = pool.submit(_call_gemini, prompt, 600)
            futures[f] = (model_name, "core")

        # Aux critics (profile-aware role)
        aux_prompt = _lang_prefix + CONTENT_CRITIC_PROMPT.format(
            task=task, content=content[:15000],
            role=aux_role,
            previously_fixed=previously_fixed or "None (first round)",
            score_schema=score_schema,
        )
        for ep_name, base, env_key, model, extra_h in available_aux:
            f = pool.submit(_call_aux_critic, ep_name, base, env_key, model, extra_h, aux_prompt)
            futures[f] = (ep_name, "aux")

        done_count = 0
        for future in concurrent.futures.as_completed(futures):
            if state.get("abort"):
                break
            name, tier = futures[future]
            done_count += 1

            if tier == "aux":
                result_name, raw = future.result()
                if not raw:
                    continue
            else:
                raw = future.result()

            parsed = _extract_json(raw) or {}
            score = _extract_score(parsed, raw) if _extract_score else 5.0

            # BUG-5 fix: 빈 응답(confidence=0, issues=[]) 감지 → 제외
            confidence = parsed.get("confidence", 1.0) if parsed else 0.0
            issues = parsed.get("issues", [])
            if confidence == 0.0 and not issues and score == 5.0:
                import sys
                sys.stderr.write(f"  [CRITIC] {name}: empty response (confidence=0, no issues) — excluded from scoring\n")
                sys.stderr.flush()
                continue

            # normalize
            normalized = _normalize_critic(parsed, name) if _normalize_critic else {"score": score, "issues": parsed.get("issues", []), "regressions": parsed.get("regressions", [])}
            results_by_model[name] = normalized
            all_scores[name] = score

            # 이슈 합산 (중복 제거)
            for iss in normalized.get("issues", []):
                key = (iss.get("summary", iss.get("desc", "")))[:40]
                if key not in seen:
                    seen.add(key)
                    all_issues.append(iss)

            state["phase_detail"] = f"{done_count}/{total_workers} critics done ({name}: {score:.1f})"
            state["messages"].append({
                "role": "critic", "model": name, "score": score,
                "label": f"Critic ({name})",
                "content": json.dumps(normalized, indent=2, ensure_ascii=False)[:8000],
                "ts": datetime.now().isoformat(),
            })

    # 점수 계산: core_min * 0.8 + aux_avg * 0.2
    core_scores = {k: v for k, v in all_scores.items() if k in critic_roles}
    aux_scores_only = {k: v for k, v in all_scores.items() if k not in critic_roles}

    core_min = min(core_scores.values()) if core_scores else 5.0
    if aux_scores_only:
        aux_avg = sum(aux_scores_only.values()) / len(aux_scores_only)
        overall = core_min * 0.8 + aux_avg * 0.2
    else:
        overall = core_min

    # 차원별 점수 (profile 차원 기준, core 최소값)
    merged_dims = {}
    profile_dims = profile["critic_dimensions"]
    for dim in profile_dims:
        vals = []
        for n_data in results_by_model.values():
            v = n_data.get("dimension_scores", {}).get(dim)
            if v is not None:
                vals.append(float(v))
        merged_dims[dim] = min(vals) if vals else 5.0

    # regressions 합산
    all_regressions = []
    for n_data in results_by_model.values():
        all_regressions.extend(n_data.get("regressions", []))

    return {
        "overall": round(overall, 1),
        "scores": merged_dims,
        "issues": all_issues,
        "regressions": all_regressions,
        "critic_scores": all_scores,
        "aux_count": len(aux_scores_only),
        "summary": next((d.get("summary", "") for d in results_by_model.values() if d.get("summary")), ""),
        "strengths": [],
    }


# ═══════════════════════════════════════════
# FINAL POLISH — profile-aware
# ═══════════════════════════════════════════

def _run_final_polish(task: str, content: str, synth_data: dict,
                     task_type: str, claude_model: str, state: dict) -> str:
    """content profile 마지막 단계: profile별 polish 규칙 적용.
    
    brainstorm: 가독성 정리만, 아이디어 삭제 금지
    portfolio: 문장 품질/응집도/톤 일관성 강화
    hybrid: 중간 수준
    """
    profile = _get_profile(task_type)
    polish_instruction = profile["polish_instruction"]

    key_messages = []
    if synth_data and isinstance(synth_data, dict):
        key_messages = synth_data.get("key_messages", [])

    lang_inst = _detect_language_instruction(task)
    prompt = (lang_inst + "\n\n" if lang_inst else "") + CONTENT_POLISH_PROMPT.format(
        task=task,
        content=content,
        polish_instruction=polish_instruction,
        key_messages=json.dumps(key_messages[:10], ensure_ascii=False) if key_messages else "None",
    )

    _record_phase_transition(state, "content:polishing")
    state["phase_detail"] = f"Final polish ({task_type} profile)"

    raw = _call_codex(prompt, 900)  # Codex로 polish (같은 모델 사용 방지)
    jd = _extract_json(raw)

    polished = content  # fallback
    if jd and "content" in jd:
        polished = jd["content"]

    state["messages"].append({
        "role": "polish", "model": "Codex",
        "content": (jd.get("changes", []) if jd else ["polish applied"])[:5],
        "ts": datetime.now().isoformat(),
    })

    return polished


# ═══════════════════════════════════════════
# UNIFIED PLANNING HARNESS
# ═══════════════════════════════════════════

def run_planning_harness(planning_id: str, task: str, task_type: str = "hybrid",
                         artifact_type: str = "doc", claude_model: str = "",
                         audience: str = "general", purpose: str = "",
                         tone: str = "professional",
                         threshold: float = 7.5, max_rounds: int = 3,
                         project_dir: str = ""):
    """Layer 3 통합 엔트리포인트.

    task_type:
      brainstorm   → content_profile only (아이디어 발산)
      portfolio    → content_profile only (내용 재해석/구조 통합)
      hybrid       → content_profile → artifact_profile (기본값)
      artifact_only → artifact_profile (이미 확정된 content가 task에 포함)
    """
    state = plannings[planning_id]
    state["_started_epoch"] = time.time()
    state["_phase_started_epoch"] = time.time()
    state["_phase_durations"] = {}
    state["_phase_avgs"] = _compute_phase_averages_from_logs()
    content = ""
    synth_data = None

    # .horcrux context 로드
    _project_context_prefix = ""
    if project_dir:
        try:
            from core.adaptive.context_loader import ProjectContext
            ctx = ProjectContext.load(project_dir)
            if ctx.loaded:
                _project_context_prefix = ctx.build_system_prefix() + "\n\n"
                # config overrides
                if ctx.get_config_override("threshold"):
                    threshold = ctx.get_config_override("threshold")
                if ctx.get_config_override("max_rounds"):
                    max_rounds = ctx.get_config_override("max_rounds")
                import sys
                sys.stderr.write(f"  [CONTEXT] Loaded .horcrux context: {ctx.to_dict()}\n")
                sys.stderr.flush()
        except Exception:
            pass
    state["_project_context_prefix"] = _project_context_prefix

    try:
        # profile 기반 threshold 조정 (brainstorm은 낮춤)
        profile = _get_profile(task_type)
        effective_threshold = threshold + profile.get("convergence_threshold_offset", 0.0)

        # ═══════════════════════════════════════
        # CONTENT PROFILE
        # ═══════════════════════════════════════
        if task_type in ("brainstorm", "portfolio", "hybrid"):
            # Phase 1: Generate (profile-aware)
            _record_phase_transition(state, "content:generating")
            state["phase_detail"] = f"3 models generating ({task_type} profile)"
            gen_results = _run_content_generators(task, claude_model, state, task_type)
            if state.get("abort"):
                state["status"] = "aborted"; return

            # Phase 2: Synthesize
            _record_phase_transition(state, "content:synthesizing")
            state["phase_detail"] = "Synthesizing 3 outputs into 1"
            content, synth_data = _run_content_synthesizer(task, gen_results, claude_model, state)
            if state.get("abort"):
                state["status"] = "aborted"; return

            # Phase 3-N: Critic → Convergence → Revision loop
            previously_fixed = []
            last_critic_merged = None
            last_diagnostics = None
            last_generator_data = synth_data

            for r in range(1, max_rounds + 1):
                if state.get("abort"):
                    break

                # Critic (profile-aware)
                _record_phase_transition(state, f"content:critic_r{r}")
                state["phase_detail"] = f"Round {r}/{max_rounds} — multi-critic ({task_type})"
                prev_text = "\n".join(previously_fixed[-15:]) if previously_fixed else "None"
                _prev_core = state.get("avg_score", 0) if r > 1 else 0.0
                critic_merged = _run_content_multi_critic(task, content, prev_text, state, task_type, prev_core_avg=_prev_core)
                if state.get("abort"):
                    break

                c_score = critic_merged["overall"]
                state["avg_score"] = c_score

                # Convergence check (profile-adjusted threshold)
                diagnostics = _check_convergence_v2(critic_merged, effective_threshold)
                last_critic_merged = critic_merged
                last_diagnostics = diagnostics

                state["messages"].append({
                    "role": "diagnostics",
                    "label": "Convergence Check",
                    "content": json.dumps(diagnostics, indent=2, ensure_ascii=False)[:5000],
                    "ts": datetime.now().isoformat(),
                })

                if diagnostics["converged"]:
                    state["phase_detail"] = f"Converged at round {r} (score {c_score:.1f})"
                    break

                # P3: resolved_ratio 기반 조기 종료 — 이슈 해결률이 낮으면 추가 라운드 무의미
                if r >= 2:
                    resolved_entries = critic_merged.get("resolved_from_previous", [])
                    if resolved_entries:
                        resolved_count = sum(1 for e in resolved_entries if e.get("status") == "resolved")
                        total_prev = len(resolved_entries)
                        resolved_ratio = resolved_count / total_prev if total_prev > 0 else 0
                        if resolved_ratio >= 0.8:
                            # 80% 이상 해결 → 수렴으로 간주
                            state["phase_detail"] = f"Resolved ratio {resolved_ratio:.0%} ≥ 80% — early exit at R{r}"
                            import sys; sys.stderr.write(f"  [P3] resolved_ratio={resolved_ratio:.0%} — early converge\n"); sys.stderr.flush()
                            break
                        elif resolved_ratio < 0.5 and r >= 2:
                            # 50% 미만 해결 + 2라운드 이상 → 추가 라운드 무의미
                            state["phase_detail"] = f"Resolved ratio {resolved_ratio:.0%} < 50% — stalled at R{r}"
                            import sys; sys.stderr.write(f"  [P3] resolved_ratio={resolved_ratio:.0%} — stalled, stopping\n"); sys.stderr.flush()
                            break

                if r >= max_rounds:
                    state["phase_detail"] = f"Max rounds reached (score {c_score:.1f})"
                    break

                # Revision
                _record_phase_transition(state, f"content:revising_r{r}")
                rev_focus = _build_revision_focus(diagnostics, critic_merged)
                ctx_pkg = _build_compact_context(
                    content[:2000], critic_merged, diagnostics, last_generator_data
                )

                # BUG-4 fix: content를 8000자로 truncate, timeout 300초, 변경분만 출력 지시
                _content_for_rev = content[:8000]
                if len(content) > 8000:
                    _content_for_rev += f"\n\n[... TRUNCATED {len(content) - 8000} chars. Focus on fixing issues above, do NOT reproduce the full document. Only output changed sections with context.]"

                _ctx_prefix = state.get("_project_context_prefix", "")
                _lang_inst = _detect_language_instruction(task)
                improve_prompt = _ctx_prefix + (_lang_inst + "\n\n" if _lang_inst else "") + CONTENT_IMPROVE_PROMPT.format(
                    task=task, content=_content_for_rev,
                    blocking_issues=_format_issues_compact(rev_focus.get("blocking_issues", [])),
                    regressions="\n".join(str(rr) for rr in rev_focus.get("regressions", [])) or "None",
                    worst_dimensions=", ".join(rev_focus.get("worst_dimensions", [])) or "None",
                    critic_disagreements="\n".join(ctx_pkg.get("critic_disagreements", [])) or "None",
                    alternative_views="\n".join(
                        (a.get("alternative", str(a)) if isinstance(a, dict) else str(a))
                        for a in ctx_pkg.get("alternative_views", [])
                    ) or "None",
                    preserve=", ".join(ctx_pkg.get("preserve", [])) or "None",
                    previously_fixed=prev_text,
                )

                raw = _call_claude(improve_prompt, 600, claude_model)
                if raw and not raw.startswith("[ERROR]") and not raw.startswith("[TIMEOUT]"):
                    jd = _extract_json(raw)
                    if jd and "content" in jd:
                        content = jd["content"]
                        last_generator_data = jd
                    elif jd and "solution" in jd:
                        content = jd["solution"]
                        last_generator_data = jd
                    else:
                        content = raw
                else:
                    # timeout/error fallback: 현재 content 유지하고 다음 라운드로
                    import sys
                    sys.stderr.write(f"  [REVISION] R{r} Claude failed: {raw[:100] if raw else 'empty'} — keeping current content\n")
                    sys.stderr.flush()

                # 이전 이슈 기록
                for iss in critic_merged.get("issues", []):
                    desc = iss.get("summary", iss.get("desc", str(iss)))
                    if desc:
                        previously_fixed.append(f"R{r}: {desc[:80]}")

                state["messages"].append({
                    "role": "revision", "round": r,
                    "label": f"Revision R{r}",
                    "content": content[:15000],
                    "ts": datetime.now().isoformat(),
                })

            # Final Polish (profile-aware)
            if not state.get("abort"):
                content = _run_final_polish(task, content, synth_data, task_type, claude_model, state)

            # Content finalize
            _record_phase_transition(state, "content:finalizing")
            state["merged_plan"] = content
            state["final_plan"] = content

        elif task_type == "artifact_only":
            # artifact_only: task 자체가 이미 확정 content
            content = task
            synth_data = None

        # ═══════════════════════════════════════
        # ARTIFACT PROFILE (hybrid 또는 artifact_only)
        # ═══════════════════════════════════════
        if task_type in ("hybrid", "artifact_only"):
            _record_phase_transition(state, "artifact:building_spec")
            state["phase_detail"] = f"Building {artifact_type} spec from content"

            content_pkg = build_content_finalize_package(
                final_content=content,
                synth_data=synth_data,
                task=task,
                artifact_goal=artifact_type,
                audience=audience,
                purpose=purpose,
                tone=tone,
            )

            # Build spec
            spec = build_artifact_spec(content_pkg, claude_model)
            state["messages"].append({
                "role": "artifact_spec",
                "label": "Artifact Spec Builder",
                "content": json.dumps(spec, indent=2, ensure_ascii=False)[:10000],
                "ts": datetime.now().isoformat(),
            })

            # Validate spec
            validation = validate_artifact_spec(spec, content_pkg)
            state["messages"].append({
                "role": "artifact_validation",
                "content": json.dumps(validation, indent=2, ensure_ascii=False),
                "ts": datetime.now().isoformat(),
            })

            # Artifact critic
            _record_phase_transition(state, "artifact:critiquing")
            state["phase_detail"] = "Artifact spec quality check"

            critic_prompt = ARTIFACT_CRITIC_PROMPT.format(
                artifact_type=artifact_type,
                content_summary=content[:3000],
                key_messages=json.dumps(content_pkg.get("key_messages", []), ensure_ascii=False),
                must_not_change=json.dumps(content_pkg.get("must_not_change", []), ensure_ascii=False),
                artifact_spec=json.dumps(spec, indent=2, ensure_ascii=False)[:10000],
            )
            critic_raw = _call_codex(critic_prompt, 600)
            critic_data = _extract_json(critic_raw) or {}
            critic_score = _extract_score(critic_data, critic_raw) if _extract_score else 5.0

            state["messages"].append({
                "role": "artifact_critic", "score": critic_score,
                "label": "Artifact Critic",
                "content": json.dumps(critic_data, indent=2, ensure_ascii=False)[:8000],
                "ts": datetime.now().isoformat(),
            })

            # Revision if needed (missing content or drift detected)
            if critic_data.get("missing_content") or critic_data.get("drift_detected") or \
               critic_data.get("must_not_change_violations"):
                _record_phase_transition(state, "artifact:revising")
                state["phase_detail"] = "Revising artifact spec based on critic feedback"

                revision_prompt = ARTIFACT_SPEC_BUILDER_PROMPT.format(
                    task=task, artifact_type=artifact_type,
                    audience=audience, purpose=purpose, tone=tone,
                    final_content=content[:15000],
                    key_messages=json.dumps(content_pkg.get("key_messages", []), ensure_ascii=False),
                    must_not_change=json.dumps(content_pkg.get("must_not_change", []), ensure_ascii=False),
                    source_decisions=json.dumps(content_pkg.get("source_decisions", [])[:5], ensure_ascii=False),
                    format_instructions=_ARTIFACT_FORMAT_MAP.get(artifact_type, DOC_FORMAT_INSTRUCTIONS),
                ) + f"\n\nCritic feedback (fix these):\n{json.dumps(critic_data, indent=2, ensure_ascii=False)[:3000]}"

                rev_raw = _call_claude(revision_prompt, 900, claude_model)
                rev_spec = _extract_json(rev_raw)
                if rev_spec and "error" not in rev_spec:
                    spec = rev_spec
                    state["messages"].append({
                        "role": "artifact_revision",
                        "label": "Artifact Revision",
                        "content": json.dumps(spec, indent=2, ensure_ascii=False)[:10000],
                        "ts": datetime.now().isoformat(),
                    })

            # Render
            _record_phase_transition(state, "artifact:rendering")
            state["phase_detail"] = f"Rendering final {artifact_type}"
            rendered = render_artifact(spec, artifact_type, claude_model)

            state["messages"].append({
                "role": "artifact_rendered",
                "label": "Artifact Rendered",
                "content": rendered[:15000],
                "ts": datetime.now().isoformat(),
            })
            state["final_solution"] = rendered
            state["artifact_spec"] = spec

            # 실제 파일 생성 (.pptx / .pdf / .md)
            try:
                from core.artifact_renderer import render_to_file
                output_dir = str(Path(_LOG_DIR).parent / "output")
                file_path = render_to_file(spec, artifact_type, output_dir)
                state["artifact_file"] = file_path
                state["messages"].append({
                    "role": "file_generated",
                    "label": f"File Generated ({artifact_type})",
                    "content": f"Saved: {file_path}",
                    "ts": datetime.now().isoformat(),
                })
                import sys
                sys.stderr.write(f"  [ARTIFACT] File saved: {file_path}\n")
                sys.stderr.flush()
            except Exception as file_err:
                import sys
                sys.stderr.write(f"  [ARTIFACT] File generation failed: {file_err}\n")
                sys.stderr.flush()

        else:
            # brainstorm/portfolio: content가 최종 결과
            state["final_solution"] = content

        state["status"] = "completed"

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        import traceback
        traceback.print_exc()

    if state.get("abort"):
        state["status"] = "aborted"

    state["finished_at"] = datetime.now().isoformat()

    # 로그 저장
    if _LOG_DIR:
        log_file = _LOG_DIR / f"{planning_id}.json"
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"  [PLAN] Log save failed: {e}")


# ═══════════════════════════════════════════
# FLASK ROUTE REGISTRATION
# ═══════════════════════════════════════════

def register_planning_v2_routes(app):
    """Flask app에 Layer 3 planning v2 라우트 등록."""
    # 이중 등록 방지
    if getattr(app, '_planning_v2_registered', False):
        return
    app._planning_v2_registered = True

    import sys
    # 순환 import 방지: python server.py로 실행 시 __main__에서 참조
    srv = sys.modules.get('__main__')
    if srv is None or not hasattr(srv, 'call_claude'):
        import server as srv

    # 의존성 주입
    inject_callers(
        call_claude=srv.call_claude,
        call_codex=srv.call_codex,
        call_gemini=srv.call_gemini,
        call_aux_critic=srv._call_aux_critic,
        aux_endpoints=srv.AUX_CRITIC_ENDPOINTS,
        extract_json_fn=srv.extract_json,
        extract_score_fn=srv.extract_score,
        normalize_critic_fn=srv.normalize_critic_output,
        check_convergence_v2_fn=srv.check_convergence_v2,
        build_revision_focus_fn=srv.build_revision_focus,
        build_compact_context_fn=srv.build_compact_context_package,
        format_issues_compact_fn=srv.format_issues_compact,
        log_dir=str(srv.LOG_DIR),
    )

    @app.route("/api/planning", methods=["POST"])
    def start_planning():
        from flask import request, jsonify
        data = request.json or {}
        task = data.get("task", "").strip()
        if not task:
            return jsonify({"error": "task required"}), 400

        planning_id = "plan_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:23]
        claude_model = srv.CLAUDE_MODELS.get(data.get("claude_model", ""), "")

        # Layer 3 parameters
        task_type = data.get("task_type", "hybrid")  # brainstorm|portfolio|hybrid|artifact_only
        artifact_type = data.get("artifact_type", "doc")  # ppt|pdf|doc|readme
        audience = data.get("audience", "general")
        purpose = data.get("purpose", task[:200])
        tone = data.get("tone", "professional")
        threshold = float(data.get("threshold", 7.5))
        max_rounds = int(data.get("max_rounds", 3))

        if task_type not in ("brainstorm", "portfolio", "hybrid", "artifact_only"):
            return jsonify({"error": f"Invalid task_type: {task_type}"}), 400
        if artifact_type not in ("ppt", "pdf", "doc", "readme"):
            return jsonify({"error": f"Invalid artifact_type: {artifact_type}"}), 400

        plannings[planning_id] = {
            "id": planning_id,
            "task": task,
            "task_type": task_type,
            "artifact_type": artifact_type,
            "status": "running",
            "phase": "starting",
            "phase_detail": "",
            "messages": [],
            "merged_plan": "",
            "final_plan": "",
            "final_solution": "",
            "artifact_spec": None,
            "avg_score": 0,
            "error": None,
            "abort": False,
            "claude_model": claude_model,
            "audience": audience,
            "purpose": purpose,
            "tone": tone,
            "created_at": datetime.now().isoformat(),
            "finished_at": None,
        }

        project_dir = data.get("project_dir", "")
        t = threading.Thread(
            target=run_planning_harness,
            args=(planning_id, task, task_type, artifact_type, claude_model,
                  audience, purpose, tone, threshold, max_rounds, project_dir),
            daemon=True,
        )
        t.start()
        return jsonify({"planning_id": planning_id, "status": "running",
                        "task_type": task_type, "artifact_type": artifact_type})

    @app.route("/api/planning/status/<planning_id>")
    def planning_status(planning_id):
        from flask import jsonify
        state = plannings.get(planning_id)
        if not state:
            log_file = srv.LOG_DIR / f"{planning_id}.json"
            if log_file.exists():
                try:
                    with open(log_file, "r", encoding="utf-8") as f:
                        state = json.load(f)
                    plannings[planning_id] = state
                except (json.JSONDecodeError, ValueError):
                    return jsonify({"error": "not found"}), 404
            else:
                return jsonify({"error": "not found"}), 404
        # timing 계산 (running일 때만)
        timing = {}
        if state.get("status") == "running" and "_started_epoch" in state:
            timing = _estimate_remaining(state, state.get("_phase_avgs", DEFAULT_PHASE_DURATIONS))

        return jsonify({
            "id": state.get("id"),
            "status": state.get("status"),
            "phase": state.get("phase", ""),
            "phase_detail": state.get("phase_detail", ""),
            "task_type": state.get("task_type", ""),
            "artifact_type": state.get("artifact_type", ""),
            "avg_score": state.get("avg_score", 0),
            "message_count": len(state.get("messages", [])),
            "created_at": state.get("created_at"),
            "finished_at": state.get("finished_at"),
            "error": state.get("error"),
            "timing": timing,
        })

    @app.route("/api/planning/result/<planning_id>")
    def planning_result(planning_id):
        from flask import jsonify
        state = plannings.get(planning_id)
        if not state:
            log_file = srv.LOG_DIR / f"{planning_id}.json"
            if log_file.exists():
                try:
                    with open(log_file, "r", encoding="utf-8") as f:
                        state = json.load(f)
                    plannings[planning_id] = state
                except (json.JSONDecodeError, ValueError):
                    return jsonify({"error": "not found"}), 404
            else:
                return jsonify({"error": "not found"}), 404
        return jsonify(state)

    @app.route("/api/planning/stop/<planning_id>", methods=["POST"])
    def planning_stop(planning_id):
        from flask import jsonify
        state = plannings.get(planning_id)
        if state:
            state["abort"] = True
        return jsonify({"ok": True})

    @app.route("/planning")
    def planning_ui():
        from flask import render_template_string
        return render_template_string(PLANNING_HTML)

    print("  [PLAN v2] Layer 3 Planning routes registered (/api/planning)")
    print(f"            task_types: brainstorm, portfolio, hybrid, artifact_only")
    print(f"            artifact_types: ppt, pdf, doc, readme")
    print(f"            UI: http://localhost:5000/planning")


# ═══════════════════════════════════════════
# PLANNING UI HTML
# ═══════════════════════════════════════════
PLANNING_HTML = r"""
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Horcrux — Planning v2</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0d1117;color:#c9d1d9;display:flex;flex-direction:column;height:100vh}
header{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:16px}
header h1{font-size:18px;color:#58a6ff;font-weight:600}
header a{color:#8b949e;text-decoration:none;font-size:13px}
header a:hover{color:#58a6ff}
.main{display:flex;flex:1;overflow:hidden}
.sidebar{width:280px;background:#161b22;border-right:1px solid #30363d;padding:16px;overflow-y:auto;flex-shrink:0}
.content{flex:1;display:flex;flex-direction:column;overflow:hidden}
.input-area{padding:16px;background:#161b22;border-bottom:1px solid #30363d}
.input-area textarea{width:100%;height:80px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:10px;font-size:14px;resize:vertical;font-family:inherit}
.input-area textarea:focus{border-color:#58a6ff;outline:none}
.controls{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center}
.controls select,.controls input{background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:6px 10px;font-size:13px}
.controls select:focus,.controls input:focus{border-color:#58a6ff;outline:none}
.controls label{font-size:12px;color:#8b949e}
btn,.btn{background:#238636;color:#fff;border:none;border-radius:6px;padding:8px 16px;font-size:14px;cursor:pointer;font-weight:500}
.btn:hover{background:#2ea043}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-stop{background:#da3633}
.btn-stop:hover{background:#f85149}
.messages{flex:1;overflow-y:auto;padding:16px}
.msg{margin-bottom:12px;padding:12px;background:#161b22;border:1px solid #30363d;border-radius:8px}
.msg-header{display:flex;justify-content:space-between;margin-bottom:6px}
.msg-role{font-weight:600;font-size:13px;text-transform:uppercase}
.msg-role.generator{color:#58a6ff}
.msg-role.synthesizer{color:#d2a8ff}
.msg-role.critic{color:#f0883e}
.msg-role.diagnostics{color:#8b949e}
.msg-role.revision{color:#7ee787}
.msg-role.artifact_spec{color:#79c0ff}
.msg-role.artifact_critic{color:#ffa657}
.msg-role.artifact_rendered{color:#56d364}
.msg-role.polish{color:#a5d6ff}
.msg-score{font-weight:700;color:#f0883e}
.msg-content{font-size:13px;white-space:pre-wrap;word-break:break-word;max-height:300px;overflow-y:auto;color:#8b949e}
.msg-ts{font-size:11px;color:#484f58}
.result-area{padding:16px;background:#0d1117;border-top:1px solid #30363d;max-height:40vh;overflow-y:auto}
.result-area pre{white-space:pre-wrap;font-size:13px;color:#c9d1d9}
.thread-item{padding:8px 12px;border-radius:6px;cursor:pointer;font-size:13px;margin-bottom:4px;color:#8b949e;border:1px solid transparent}
.thread-item:hover{background:#1c2128;border-color:#30363d}
.thread-item.active{background:#1c2128;border-color:#58a6ff;color:#c9d1d9}
.thread-item .t-status{font-size:11px;margin-top:2px}
.badge{display:inline-block;padding:2px 6px;border-radius:10px;font-size:11px;font-weight:600}
.badge-running{background:#1f6feb33;color:#58a6ff}
.badge-completed{background:#23863633;color:#56d364}
.badge-error{background:#da363333;color:#f85149}
.empty{text-align:center;color:#484f58;padding:40px;font-size:14px}
.phase-bar{padding:8px 16px;background:#1c2128;border-bottom:1px solid #30363d;font-size:13px;color:#58a6ff;display:none}
</style>
</head>
<body>
<header>
  <h1>Planning Pipeline v2</h1>
  <a href="/">← Debate UI</a>
  <span style="color:#484f58;font-size:12px">Layer 3: Content Profile + Artifact Profile</span>
</header>
<div class="main">
  <div class="sidebar" id="sidebar">
    <div style="margin-bottom:12px;font-size:12px;color:#484f58">History</div>
    <div id="threads"></div>
  </div>
  <div class="content">
    <div class="input-area">
      <textarea id="taskInput" placeholder="Planning task... (e.g. WSY 앱 리텐션 개선 전략 기획 → PPT)"></textarea>
      <div class="controls">
        <div><label>Type</label><br>
          <select id="taskType"><option value="hybrid">Hybrid (Content→Artifact)</option><option value="brainstorm">Brainstorm (Ideas)</option><option value="portfolio">Portfolio (Restructure)</option><option value="artifact_only">Artifact Only (Render)</option></select>
        </div>
        <div><label>Artifact</label><br>
          <select id="artifactType"><option value="doc">Document</option><option value="ppt">PPT Slides</option><option value="pdf">PDF</option><option value="readme">README</option></select>
        </div>
        <div><label>Audience</label><br>
          <input id="audience" value="general" style="width:100px">
        </div>
        <div><label>Tone</label><br>
          <select id="tone"><option value="professional">Professional</option><option value="casual">Casual</option><option value="technical">Technical</option></select>
        </div>
        <div><label>Model</label><br>
          <select id="claudeModel"><option value="">Auto (Opus)</option><option value="opus">Opus 4.6</option><option value="sonnet">Sonnet 4.6</option></select>
        </div>
        <div style="display:flex;gap:6px;align-items:end">
          <button class="btn" id="btnRun" onclick="startPlanning()">▶ Run Planning</button>
          <button class="btn btn-stop" id="btnStop" style="display:none" onclick="stopPlanning()">■ Stop</button>
        </div>
      </div>
    </div>
    <div class="phase-bar" id="phaseBar"></div>
    <div class="messages" id="messages"><div class="empty" id="emptyState">Enter a task and click Run Planning</div></div>
    <div class="result-area" id="resultArea" style="display:none"><h3 style="margin-bottom:8px;color:#56d364">Final Output</h3><pre id="resultText"></pre></div>
  </div>
</div>
<script>
let cid=null,pt=null,lmc=0,run=false;
function $(id){return document.getElementById(id)}
function esc(t){return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

async function startPlanning(){
  const task=$('taskInput').value.trim();
  if(!task)return;
  $('btnRun').disabled=true;$('btnStop').style.display='inline-block';
  $('messages').innerHTML='';$('resultArea').style.display='none';$('emptyState').style.display='none';
  $('phaseBar').style.display='block';$('phaseBar').textContent='Starting...';
  lmc=0;run=true;
  try{
    const r=await fetch('/api/planning',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      task,task_type:$('taskType').value,artifact_type:$('artifactType').value,
      audience:$('audience').value,tone:$('tone').value,claude_model:$('claudeModel').value
    })});
    const d=await r.json();
    cid=d.planning_id;
    if(!cid){$('phaseBar').textContent='Error: '+JSON.stringify(d);run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';return;}
    if(pt)clearInterval(pt);
    pt=setInterval(poll,2000);
    loadThreads();
  }catch(e){$('phaseBar').textContent='Error: '+e;run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';}
}

async function poll(){
  if(!cid||!run)return;
  try{
    const sr=await(await fetch('/api/planning/status/'+cid)).json();
    const t = sr.timing || {};
    let timeStr = '';
    if (t.elapsed_sec) {
        const elapsed = Math.floor(t.elapsed_sec/60)+':'+(t.elapsed_sec%60+'').padStart(2,'0');
        const eta = t.eta_remaining_sec ? '~'+Math.floor(t.eta_remaining_sec/60)+':'+(t.eta_remaining_sec%60+'').padStart(2,'0') : '?';
        timeStr = ' | \u23f1 '+elapsed+' / ETA '+eta+' | '+t.progress_pct+'%';
    }
    $('phaseBar').textContent=sr.phase+' | '+sr.phase_detail + timeStr + (sr.avg_score?' | Score: '+sr.avg_score+'/10':'');
    if(sr.status!=='running'){
      clearInterval(pt);pt=null;run=false;
      $('btnRun').disabled=false;$('btnStop').style.display='none';
      $('phaseBar').textContent=sr.status.toUpperCase()+' | '+sr.phase_detail;
    }
    // messages
    const fr=await(await fetch('/api/planning/result/'+cid)).json();
    const msgs=fr.messages||[];
    if(msgs.length>lmc){
      for(let i=lmc;i<msgs.length;i++)addMsg(msgs[i]);
      lmc=msgs.length;
    }
    if(sr.status!=='running'&&fr.final_solution){
      $('resultArea').style.display='block';
      $('resultText').textContent=fr.final_solution;
    }
    loadThreads();
  }catch(e){console.error(e);}
}

function addMsg(m){
  const d=document.createElement('div');d.className='msg';
  const role=m.role||'?';
  const displayName=m.label||role;
  let h='<div class="msg-header"><span class="msg-role '+role+'">'+esc(displayName);
  h+='</span>';
  if(m.score)h+='<span class="msg-score">'+m.score+'/10</span>';
  h+='</div>';
  h+='<div class="msg-content">'+esc(typeof m.content==='string'?m.content:JSON.stringify(m.content))+'</div>';
  if(m.ts)h+='<div class="msg-ts">'+m.ts+'</div>';
  d.innerHTML=h;
  $('messages').appendChild(d);
  $('messages').scrollTop=$('messages').scrollHeight;
}

async function stopPlanning(){
  if(!cid)return;
  await fetch('/api/planning/stop/'+cid,{method:'POST'});
}

async function loadThreads(){
  try{
    // 로그 디렉토리에서 plan_ 로 시작하는 것들
    const r=await fetch('/api/threads');
    const threads=await r.json();
    const plans=threads.filter(t=>(t.id||'').startsWith('plan_'));
    const el=$('threads');
    el.innerHTML=plans.slice(0,20).map(t=>{
      const active=t.id===cid?'active':'';
      const badge=t.status==='completed'?'completed':t.status==='running'?'running':'error';
      return '<div class="thread-item '+active+'" onclick="loadPlan(\''+t.id+'\')">' +
        '<div>'+esc((t.task||t.id||'').slice(0,50))+'</div>' +
        '<div class="t-status"><span class="badge badge-'+badge+'">'+esc(t.status||'?')+'</span> '+(t.avg_score?t.avg_score+'/10':'')+'</div></div>';
    }).join('');
  }catch(e){}
}

async function loadPlan(id){
  cid=id;
  if(pt)clearInterval(pt);
  run=false;lmc=0;
  $('messages').innerHTML='';$('resultArea').style.display='none';$('emptyState').style.display='none';
  $('phaseBar').style.display='block';$('phaseBar').textContent='Loading...';
  try{
    const fr=await(await fetch('/api/planning/result/'+id)).json();
    if(fr.task)$('taskInput').value=fr.task;
    if(fr.task_type)$('taskType').value=fr.task_type;
    if(fr.artifact_type)$('artifactType').value=fr.artifact_type;
    const msgs=fr.messages||[];
    msgs.forEach(m=>addMsg(m));
    lmc=msgs.length;
    $('phaseBar').textContent=(fr.status||'?').toUpperCase()+' | '+(fr.phase_detail||fr.phase||'');
    if(fr.final_solution){$('resultArea').style.display='block';$('resultText').textContent=fr.final_solution;}
    if(fr.status==='running'){run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';pt=setInterval(poll,2000);}
    else{$('btnRun').disabled=false;$('btnStop').style.display='none';}
    loadThreads();
  }catch(e){$('phaseBar').textContent='Error: '+e;}
}

loadThreads();
</script>
</body>
</html>
"""