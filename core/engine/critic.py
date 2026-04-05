"""
core/engine/critic.py — R19: Critic normalization, convergence, revision logic

server.py에서 추출한 순수 도메인 함수들. HTTP/상태 의존 없음.
"""

import json
import re


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


