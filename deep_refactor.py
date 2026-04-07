"""
Horcrux -Deep Refactoring Pipeline v2

5-Phase Multi-Model Code Refactoring:
  Phase 0: Auto-Split (file tree → Claude가 모듈 그룹 자동 분할)
  Phase 1: Parallel Analysis (그룹별 × 3모델 병렬 분석)
  Phase 2: Synthesis (모든 그룹 분석 → 1개 통합 리팩토링 플랜)
  Phase 3: Critic-Revision Loop (5개 모델 크리틱 → 리비전 반복)
  Phase 4: Final Output

특징:
  - 대형 프로젝트를 모듈 단위로 자동 분할 → 55K 제한 극복
  - 그룹별 3모델 독립 분석 (예: 4그룹 × 3모델 = 12 병렬)
  - 5개 모델 크리틱으로 깊은 검증
"""

import json
import os
import concurrent.futures
from datetime import datetime
from pathlib import Path

# ─── 의존성 주입 (server.py에서 호출) ───
_call_claude = None
_call_codex = None
_call_gemini = None
_call_aux_critic = None
_AUX_CRITIC_ENDPOINTS = None
_extract_json = None
_extract_score = None
_LOG_DIR = None


def inject_callers(call_claude, call_codex, call_gemini,
                   call_aux_critic_fn, aux_endpoints,
                   extract_json_fn, extract_score_fn, log_dir):
    """server.py에서 호출하여 의존성 주입."""
    global _call_claude, _call_codex, _call_gemini
    global _call_aux_critic, _AUX_CRITIC_ENDPOINTS
    global _extract_json, _extract_score, _LOG_DIR
    _call_claude = call_claude
    _call_codex = call_codex
    _call_gemini = call_gemini
    _call_aux_critic = call_aux_critic_fn
    _AUX_CRITIC_ENDPOINTS = aux_endpoints
    _extract_json = extract_json_fn
    _extract_score = extract_score_fn
    _LOG_DIR = Path(log_dir)


# ─── 상태 저장소 ───
deep_refactors = {}

# ─── 설정 ───
GROUP_MAX_CHARS = 50000  # 그룹당 소스코드 최대 크기
AUX_MAX_CHARS = 12000    # Aux 모델용
GEN_TIMEOUT = 900        # 15분 per model
CRITIC_TIMEOUT = 300     # 5분 per critic
MAX_GROUPS = 6           # 최대 그룹 수
MAX_PARALLEL_WORKERS = 12  # Phase 1 최대 동시 실행 수

SKIP_DIRS = {
    "__pycache__", ".venv", "venv", "node_modules", ".git",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    "egg-info", ".eggs", "migrations",
}
SOURCE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php",
    ".yaml", ".yml", ".toml", ".json", ".md",
}


# ═══════════════════════════════════════════
# 프로젝트 파일 읽기
# ═══════════════════════════════════════════

def _scan_project(project_dir: str) -> tuple:
    """프로젝트 파일 목록과 트리 반환. (all_files, file_tree)"""
    base = Path(project_dir)
    if not base.exists():
        return [], ""

    all_files = []
    for f in sorted(base.rglob("*")):
        if f.is_dir():
            continue
        if any(skip in f.parts for skip in SKIP_DIRS):
            continue
        if f.suffix.lower() not in SOURCE_EXTS:
            continue
        all_files.append(f)

    tree_lines = []
    for f in all_files:
        rel = str(f.relative_to(base)).replace("\\", "/")
        size = f.stat().st_size
        tree_lines.append(f"  {rel} ({size}B)")
    file_tree = f"Project: {base.name}/\n" + "\n".join(tree_lines)

    return all_files, file_tree


def _read_files(base: Path, file_paths: list, max_chars: int = GROUP_MAX_CHARS) -> str:
    """지정된 파일들의 소스코드를 읽어서 반환."""
    # 크기 순 정렬 (큰 파일 = 핵심)
    file_paths = sorted(file_paths, key=lambda f: f.stat().st_size, reverse=True)

    chunks = []
    total = 0
    for f in file_paths:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            rel = str(f.relative_to(base)).replace("\\", "/")
            entry = f"\n### {rel}\n```\n{content}\n```"
            if total + len(entry) > max_chars:
                remain = max_chars - total - 100
                if remain > 500 and not chunks:
                    chunks.append(f"\n### {rel}\n```\n{content[:remain]}\n[...truncated]\n```")
                break
            chunks.append(entry)
            total += len(entry)
        except Exception:
            continue

    return "\n".join(chunks)


# ═══════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════

SPLIT_PROMPT = """You are a software architect. Analyze this project's file tree and split it into logical module groups for independent refactoring analysis.

Task context: {task}

=== File Tree (with sizes) ===
{file_tree}

Total project size: {total_size} chars across {file_count} files.

Rules:
1. Each group should be a cohesive module/feature area that can be analyzed independently
2. Target {target_groups} groups (adjust if the project is smaller/larger)
3. Large files (>30KB) can be their own group
4. Keep tightly coupled files together (e.g., a module and its tests, a class and its config)
5. Each group's total source size should fit within ~50K chars
6. Every file must belong to exactly one group

Reply JSON only:
{{"groups":[{{"id":"group1","name":"<descriptive name>","description":"<what this module does>","files":["<relative/path/to/file>"]}}],"rationale":"<why you split this way>"}}"""

ANALYSIS_PROMPT = """You are a {role} expert performing a deep code analysis.

Task context: {task}
Module: {group_name} -{group_description}

=== Full Project Structure (for cross-reference context) ===
{file_tree}

=== Source Code (this module only) ===
{source_code}

Analyze this module from your {role} perspective. Find ALL refactoring opportunities.
Consider both internal issues AND cross-module concerns (imports, coupling, interfaces).

For each issue found, provide:
- category: architecture | code_quality | performance | security | maintainability | testability
- severity: critical | high | medium | low
- files: affected file paths
- description: what the problem is
- suggestion: how to fix it
- effort: S (< 1hr) | M (1-4hr) | L (4hr+)

Reply JSON only:
{{"module":"{group_name}","role":"{role}","total_issues":<count>,"issues":[{{"category":"<cat>","severity":"<sev>","files":["<path>"],"description":"<desc>","suggestion":"<fix>","effort":"<S|M|L>"}}],"architecture_notes":"<how this module fits in the overall system>","top_priorities":["top 3 most impactful changes for this module"]}}"""

MEGA_SYNTHESIS_PROMPT = """You are a principal engineer synthesizing refactoring analyses from multiple module groups, each reviewed by 3 independent experts.

Task context: {task}

=== Full Project Structure ===
{file_tree}

=== Module Group Analyses ===
{all_analyses}

Instructions:
1. Merge ALL issues across ALL groups -deduplicate where same issue found in multiple modules
2. Identify CROSS-MODULE issues (coupling, circular imports, inconsistent patterns across modules)
3. When experts disagree on severity, take the HIGHER severity
4. Create a unified, prioritized refactoring plan covering the ENTIRE project
5. Group by implementation order (what to do first → last, respecting dependencies)
6. Flag issues that require coordinated changes across multiple modules

Reply JSON only:
{{"total_issues":<count>,"modules_analyzed":<count>,"implementation_phases":[{{"phase":"<name>","description":"<what and why>","issues":[{{"id":"R<num>","category":"<cat>","severity":"<sev>","files":["<paths>"],"description":"<desc>","suggestion":"<fix>","effort":"<S|M|L>","module":"<which group>","cross_module":<true|false>}}]}}],"cross_module_concerns":["issues spanning multiple modules"],"architecture_summary":"<current state + target state>","estimated_total_effort":"<total time estimate>","risk_notes":["risks of refactoring"]}}"""

CRITIC_PROMPT = """You are a {role} critic reviewing a code refactoring plan.

Task context: {task}

=== Project Structure ===
{file_tree}

=== Refactoring Plan ===
{plan}

Evaluate this refactoring plan from your {role} perspective:
1. Are the identified issues real and important?
2. Are the suggested fixes correct and complete?
3. Are there missed issues the plan should address?
4. Is the priority ordering correct?
5. Are there risks or side effects not mentioned?
6. Is the effort estimation realistic?

Reply JSON only:
{{"overall_score":<1-10>,"scores":{{"accuracy":<1-10>,"completeness":<1-10>,"feasibility":<1-10>,"priority_ordering":<1-10>}},"missed_issues":["issue not covered"],"incorrect_items":["item that is wrong or unnecessary"],"suggested_changes":["concrete improvement"],"risk_warnings":["risk not mentioned"],"strengths":["what the plan gets right"]}}"""

REVISION_PROMPT = """You are a principal engineer revising a refactoring plan based on multi-model critique.

Task context: {task}

=== Current Refactoring Plan ===
{plan}

=== Critique Feedback (from {num_critics} models) ===
{critique_feedback}

Instructions:
1. Address every issue raised by critics
2. Add missed issues that critics identified
3. Remove or fix items critics flagged as incorrect
4. Adjust priorities based on critic feedback
5. Keep all strengths intact
6. Produce the COMPLETE revised plan -do not truncate

Reply JSON only (same structure as the original plan, but improved):
{{"total_issues":<count>,"modules_analyzed":<count>,"implementation_phases":[{{"phase":"<name>","description":"<what and why>","issues":[{{"id":"R<num>","category":"<cat>","severity":"<sev>","files":["<paths>"],"description":"<desc>","suggestion":"<fix>","effort":"<S|M|L>","module":"<group>","cross_module":<true|false>}}]}}],"cross_module_concerns":["issues spanning modules"],"architecture_summary":"<current + target>","estimated_total_effort":"<total>","risk_notes":["risks"],"revision_notes":["what changed from previous version"]}}"""


# ═══════════════════════════════════════════
# PIPELINE
# ═══════════════════════════════════════════

def run_deep_refactor(refactor_id, task, project_dir, claude_model="opus",
                      threshold=7.5, max_rounds=3):
    """5-phase deep refactoring pipeline with auto-split."""
    state = deep_refactors[refactor_id]

    try:
        # ══════════════════════════════════════
        # Phase 0: 프로젝트 스캔 + Auto-Split
        # ══════════════════════════════════════
        state["phase"] = "scanning"
        base = Path(project_dir)
        all_files, file_tree = _scan_project(project_dir)

        if not all_files:
            state["status"] = "error"
            state["error"] = f"No source files found in {project_dir}"
            _save_log(refactor_id, state)
            return

        total_size = sum(f.stat().st_size for f in all_files)
        file_count = len(all_files)

        state["messages"].append({
            "role": "system",
            "content": f"Scanned {file_count} files, {total_size:,} chars total from {project_dir}",
            "ts": datetime.now().isoformat(),
        })

        # 프로젝트 크기에 따라 그룹 수 결정
        if total_size <= GROUP_MAX_CHARS:
            # 작은 프로젝트 → 그룹 분할 불필요, 단일 그룹
            groups = [{"id": "group1", "name": "entire_project", "description": "All project files", "files": [str(f.relative_to(base)).replace("\\", "/") for f in all_files]}]
            state["messages"].append({
                "role": "system",
                "content": f"Small project ({total_size:,} chars) -single group, no split needed",
                "ts": datetime.now().isoformat(),
            })
        else:
            # 큰 프로젝트 → Claude가 자동 분할
            state["phase"] = "auto_split"
            target_groups = min(MAX_GROUPS, max(2, total_size // GROUP_MAX_CHARS + 1))
            state["phase_detail"] = f"Splitting into ~{target_groups} groups"

            split_raw = _call_claude(SPLIT_PROMPT.format(
                task=task,
                file_tree=file_tree,
                total_size=f"{total_size:,}",
                file_count=file_count,
                target_groups=target_groups,
            ))

            split_json = _extract_json(split_raw) if split_raw else None
            if split_json and "groups" in split_json:
                groups = split_json["groups"]
                rationale = split_json.get("rationale", "")
            else:
                # fallback: 디렉토리 기반 자동 분할
                groups = _fallback_split(base, all_files)
                rationale = "fallback: directory-based split"

            state["messages"].append({
                "role": "architect",
                "model": "Claude",
                "content": json.dumps({"groups": [{"id": g["id"], "name": g["name"], "file_count": len(g["files"])} for g in groups], "rationale": rationale}, ensure_ascii=False),
                "ts": datetime.now().isoformat(),
            })

            print(f"[DRF] Split into {len(groups)} groups: {[g['name'] for g in groups]}")

        if state.get("abort"):
            state["status"] = "aborted"
            _save_log(refactor_id, state)
            return

        # ══════════════════════════════════════
        # Phase 1: 그룹별 × 3모델 병렬 분석
        # ══════════════════════════════════════
        state["phase"] = "parallel_analysis"
        num_analyses = len(groups) * 3
        state["phase_detail"] = f"{len(groups)} groups × 3 models = {num_analyses} parallel analyses"

        model_roles = [
            ("system_architect", "System Architecture / structure / coupling / cohesion", _call_claude),
            ("senior_developer", "Code Quality / duplication / naming / complexity / bugs", _call_codex),
            ("dx_maintainability", "Maintainability / Developer Experience / testability / readability", _call_gemini),
        ]

        all_analyses = {}  # {group_id: {role_id: raw_text}}

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(num_analyses, MAX_PARALLEL_WORKERS)) as pool:
            futures = []

            for group in groups:
                group_id = group["id"]
                group_name = group["name"]
                group_desc = group.get("description", "")
                group_files_rel = group["files"]

                # 그룹 파일 매칭
                group_file_paths = []
                for f in all_files:
                    rel = str(f.relative_to(base)).replace("\\", "/")
                    if rel in group_files_rel:
                        group_file_paths.append(f)

                # 그룹 소스코드 읽기
                group_source = _read_files(base, group_file_paths, GROUP_MAX_CHARS)
                if not group_source.strip():
                    continue

                all_analyses[group_id] = {}

                for role_id, role_desc, caller in model_roles:
                    prompt = ANALYSIS_PROMPT.format(
                        role=role_desc,
                        task=task,
                        group_name=group_name,
                        group_description=group_desc,
                        file_tree=file_tree,
                        source_code=group_source,
                    )
                    futures.append((group_id, group_name, role_id, caller, pool.submit(caller, prompt)))

            # 결과 수집
            completed = 0
            for group_id, group_name, role_id, caller, future in futures:
                if state.get("abort"):
                    break
                try:
                    raw = future.result(timeout=GEN_TIMEOUT)
                    all_analyses[group_id][role_id] = raw or ""
                    parsed = _extract_json(raw) if raw else None
                    issue_count = parsed.get("total_issues", "?") if parsed else "?"
                    completed += 1

                    state["messages"].append({
                        "role": f"analyst",
                        "model": role_id,
                        "group": group_name,
                        "content": raw or "[NO OUTPUT]",
                        "issue_count": issue_count,
                        "ts": datetime.now().isoformat(),
                    })
                    state["phase_detail"] = f"Completed {completed}/{num_analyses}"
                    print(f"[DRF] {group_name}/{role_id}: {issue_count} issues")

                except Exception as e:
                    completed += 1
                    print(f"[DRF] {group_name}/{role_id} failed: {e}")
                    all_analyses[group_id][role_id] = f"[FAILED] {e}"

        if state.get("abort"):
            state["status"] = "aborted"
            _save_log(refactor_id, state)
            return

        # 유효한 분석 결과 확인
        valid_count = sum(
            1 for gid in all_analyses
            for rid, txt in all_analyses[gid].items()
            if not txt.startswith("[FAILED]")
        )
        if valid_count < 2:
            state["status"] = "error"
            state["error"] = f"Only {valid_count} valid analyses (need >= 2)"
            _save_log(refactor_id, state)
            return

        # ══════════════════════════════════════
        # Phase 2: 전체 종합 (모든 그룹 분석 → 1개 통합 플랜)
        # ══════════════════════════════════════
        state["phase"] = "synthesis"
        state["phase_detail"] = f"Merging {valid_count} analyses from {len(groups)} groups"

        # 모든 분석 결과를 포맷
        analyses_text = ""
        for group in groups:
            gid = group["id"]
            gname = group["name"]
            if gid not in all_analyses:
                continue
            analyses_text += f"\n\n{'='*40}\nMODULE: {gname}\n{'='*40}\n"
            for role_id, raw in all_analyses[gid].items():
                if raw.startswith("[FAILED]"):
                    continue
                # 각 분석을 적당히 잘라서 포함 (synthesis 프롬프트 폭발 방지)
                trimmed = raw[:15000] if len(raw) > 15000 else raw
                analyses_text += f"\n--- {role_id} ---\n{trimmed}\n"

        synth_prompt = MEGA_SYNTHESIS_PROMPT.format(
            task=task,
            file_tree=file_tree,
            all_analyses=analyses_text,
        )

        plan_raw = _call_claude(synth_prompt, model=claude_model)
        plan_json = _extract_json(plan_raw) if plan_raw else None
        plan_text = json.dumps(plan_json, indent=2, ensure_ascii=False) if plan_json else plan_raw

        state["messages"].append({
            "role": "synthesizer",
            "model": f"Claude {claude_model}",
            "content": plan_text or "[NO OUTPUT]",
            "ts": datetime.now().isoformat(),
        })

        if not plan_text:
            state["status"] = "error"
            state["error"] = "Synthesis failed to produce output"
            _save_log(refactor_id, state)
            return

        if state.get("abort"):
            state["status"] = "aborted"
            _save_log(refactor_id, state)
            return

        # ══════════════════════════════════════
        # Phase 3: 크리틱-리비전 루프
        # ══════════════════════════════════════
        current_plan = plan_text

        for round_num in range(1, max_rounds + 1):
            state["phase"] = "critic_revision"
            state["phase_detail"] = f"Round {round_num}/{max_rounds}"
            state["round"] = round_num

            critic_results = _run_multi_critic(
                task=task,
                plan=current_plan,
                file_tree=file_tree,
                state=state,
                round_num=round_num,
            )

            if state.get("abort"):
                break

            scores = [c["score"] for c in critic_results if c.get("score", 0) > 0]
            avg_score = sum(scores) / len(scores) if scores else 0
            state["avg_score"] = round(avg_score, 1)

            print(f"[DRF] Round {round_num}: avg_score={avg_score:.1f} (threshold={threshold})")

            if avg_score >= threshold:
                state["status"] = "converged"
                state["final_solution"] = current_plan
                break

            if round_num < max_rounds:
                state["phase_detail"] = f"Round {round_num}/{max_rounds} -revising"
                critique_text = _format_critiques(critic_results)

                rev_prompt = REVISION_PROMPT.format(
                    task=task,
                    plan=current_plan,
                    critique_feedback=critique_text,
                    num_critics=len(critic_results),
                )

                rev_raw = _call_claude(rev_prompt, model=claude_model)
                rev_json = _extract_json(rev_raw) if rev_raw else None
                current_plan = json.dumps(rev_json, indent=2, ensure_ascii=False) if rev_json else (rev_raw or current_plan)

                state["messages"].append({
                    "role": "revision",
                    "model": f"Claude {claude_model}",
                    "round": round_num,
                    "content": current_plan,
                    "ts": datetime.now().isoformat(),
                })

        # ══════════════════════════════════════
        # Phase 3.5: 이슈 검증 패스 (오진 방지)
        # ══════════════════════════════════════
        state["phase"] = "verification"
        state["phase_detail"] = "Verifying issues against actual code"
        current_plan = _verify_issues(current_plan, project_dir, state, claude_model)

        # ══════════════════════════════════════
        # Phase 4: 최종 결과
        # ══════════════════════════════════════
        if state["status"] == "running":
            state["status"] = "max_rounds"
        state["final_solution"] = current_plan

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)

    if state.get("abort"):
        state["status"] = "aborted"

    state["finished_at"] = datetime.now().isoformat()
    _save_log(refactor_id, state)

    if state["status"] in ("converged", "max_rounds", "completed"):
        try:
            from server import _maybe_auto_tune_scoring
            _maybe_auto_tune_scoring()
        except ImportError:
            pass


# ═══════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════

def _fallback_split(base: Path, all_files: list) -> list:
    """Claude 분할 실패 시 디렉토리 기반 자동 분할."""
    dir_groups = {}
    for f in all_files:
        rel = str(f.relative_to(base)).replace("\\", "/")
        parts = rel.split("/")
        # 1단계 디렉토리 기준, 루트 파일은 "root"로
        group_key = parts[0] if len(parts) > 1 and not parts[0].endswith(('.py', '.js', '.ts')) else "root"
        if group_key not in dir_groups:
            dir_groups[group_key] = []
        dir_groups[group_key].append(rel)

    groups = []
    for i, (dir_name, files) in enumerate(dir_groups.items()):
        groups.append({
            "id": f"group{i+1}",
            "name": dir_name,
            "description": f"Files in {dir_name}/",
            "files": files,
        })

    # 그룹이 너무 많으면 작은 그룹끼리 병합
    if len(groups) > MAX_GROUPS:
        groups.sort(key=lambda g: len(g["files"]))
        while len(groups) > MAX_GROUPS:
            smallest = groups.pop(0)
            groups[0]["files"].extend(smallest["files"])
            groups[0]["name"] += f"+{smallest['name']}"

    return groups


def _run_multi_critic(task, plan, file_tree, state, round_num):
    """5개 모델 크리틱 병렬 실행."""
    critic_results = []

    core_roles = [
        ("accuracy_expert", _call_claude, "Claude"),
        ("code_quality_expert", _call_codex, "Codex"),
        ("feasibility_expert", _call_gemini, "Gemini"),
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = []

        for role_name, caller, model_name in core_roles:
            prompt = CRITIC_PROMPT.format(
                role=role_name,
                task=task,
                file_tree=file_tree,
                plan=plan,
            )
            futures.append((model_name, role_name, pool.submit(caller, prompt)))

        if _AUX_CRITIC_ENDPOINTS:
            aux_plan = plan[:AUX_MAX_CHARS] if len(plan) > AUX_MAX_CHARS else plan
            aux_prompt = CRITIC_PROMPT.format(
                role="general_reviewer",
                task=task,
                file_tree=file_tree[:3000],
                plan=aux_plan,
            )
            for ep_tuple in _AUX_CRITIC_ENDPOINTS:
                ep_name, base_url, env_key, model, extra_h = ep_tuple
                if not os.environ.get(env_key):
                    continue
                futures.append((ep_name, "aux_critic", pool.submit(
                    _call_aux_critic, ep_name, base_url, env_key, model, extra_h, aux_prompt
                )))

        for model_name, role_name, future in futures:
            try:
                result = future.result(timeout=CRITIC_TIMEOUT)
                if isinstance(result, tuple):
                    _, raw = result
                else:
                    raw = result
                parsed = _extract_json(raw) if raw else None
                score = 0
                if parsed:
                    score = parsed.get("overall_score", 0)
                    if not score:
                        score = _extract_score(parsed, raw)

                critic_results.append({
                    "model": model_name,
                    "role": role_name,
                    "score": score,
                    "raw": raw,
                    "parsed": parsed,
                })

                state["messages"].append({
                    "role": "critic",
                    "model": model_name,
                    "round": round_num,
                    "score": score,
                    "content": raw or "[NO OUTPUT]",
                    "ts": datetime.now().isoformat(),
                })

                print(f"[DRF] Critic {model_name}: {score}/10")

            except Exception as e:
                print(f"[DRF] Critic {model_name} failed: {e}")
                state["messages"].append({
                    "role": "critic",
                    "model": model_name,
                    "round": round_num,
                    "score": 0,
                    "content": f"[FAILED] {e}",
                    "ts": datetime.now().isoformat(),
                })

    return critic_results


def _format_critiques(critic_results):
    """크리틱 결과를 텍스트로 포맷."""
    lines = []
    for c in critic_results:
        model = c.get("model", "unknown")
        score = c.get("score", 0)
        parsed = c.get("parsed")
        if parsed:
            lines.append(f"\n--- {model} (score: {score}/10) ---")
            if parsed.get("missed_issues"):
                lines.append(f"Missed issues: {json.dumps(parsed['missed_issues'], ensure_ascii=False)}")
            if parsed.get("incorrect_items"):
                lines.append(f"Incorrect items: {json.dumps(parsed['incorrect_items'], ensure_ascii=False)}")
            if parsed.get("suggested_changes"):
                lines.append(f"Suggested changes: {json.dumps(parsed['suggested_changes'], ensure_ascii=False)}")
            if parsed.get("risk_warnings"):
                lines.append(f"Risk warnings: {json.dumps(parsed['risk_warnings'], ensure_ascii=False)}")
        else:
            raw = c.get("raw", "")
            lines.append(f"\n--- {model} (score: {score}/10) ---\n{raw[:2000]}")
    return "\n".join(lines)


def _save_log(refactor_id, state):
    """로그 저장."""
    if _LOG_DIR:
        log_file = _LOG_DIR / f"{refactor_id}.json"
        try:
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            print(f"[DRF] Log save error: {e}")


def create_state(refactor_id, task, project_dir):
    """초기 상태 생성."""
    state = {
        "id": refactor_id,
        "task": task,
        "project_dir": project_dir,
        "status": "running",
        "phase": "init",
        "phase_detail": "",
        "round": 0,
        "messages": [],
        "avg_score": 0,
        "final_solution": None,
        "error": None,
        "abort": False,
        "created_at": datetime.now().isoformat(),
        "finished_at": None,
    }
    deep_refactors[refactor_id] = state
    return state


# ═══════════════════════════════════════════
# Phase 3.5: Issue Verification (오진 방지)
# ═══════════════════════════════════════════

VERIFY_PROMPT = """You are a code audit verifier. You were given a refactoring plan with issues.
Now you have the ACTUAL source code of the files mentioned in each issue.

## Task
For each issue below, verify whether it is:
- **CONFIRMED** -the issue exists in the actual code
- **FALSE_POSITIVE** -the issue does NOT exist (code already handles it correctly)
- **PARTIALLY_TRUE** -the issue exists but is less severe than described

## Issues to verify
{issues_json}

## Actual source code
{source_code}

## Instructions
1. Read each issue carefully
2. Find the relevant code in the source files
3. Check if the described problem actually exists
4. Mark false positives clearly

## Output
Return the SAME JSON structure as the input plan, but:
- Add "verified": "CONFIRMED" | "FALSE_POSITIVE" | "PARTIALLY_TRUE" to each issue
- Add "verification_note": "<why>" to each issue
- Remove issues marked FALSE_POSITIVE from the final plan
- Keep the overall structure (phases, cross_module_concerns, etc.)

Respond with ONLY the corrected JSON."""


def _verify_issues(plan_text: str, project_dir: str, state: dict, claude_model: str = "") -> str:
    """
    Phase 3.5: 이슈에서 언급한 파일을 실제로 읽고 Claude에게 검증 요청.
    오진(false positive)을 제거하여 최종 결과 품질 향상.
    """
    base = Path(project_dir)
    if not base.exists():
        return plan_text

    # plan에서 언급된 파일 목록 추출
    try:
        plan_json = json.loads(plan_text) if isinstance(plan_text, str) else plan_text
    except (json.JSONDecodeError, TypeError):
        print("[DRF] Verification skipped -plan is not valid JSON")
        return plan_text

    mentioned_files = set()
    phases = plan_json.get("implementation_phases", [])
    for phase in phases:
        for issue in phase.get("issues", []):
            for f in issue.get("files", []):
                mentioned_files.add(f)

    if not mentioned_files:
        return plan_text

    # 언급된 파일들의 실제 코드 읽기 (전체 내용)
    source_chunks = []
    total = 0
    MAX_VERIFY_CHARS = 80000  # 검증용은 넉넉하게

    for rel_path in sorted(mentioned_files):
        candidates = [
            base / rel_path,
            base / rel_path.replace("/", "\\"),
        ]
        for fpath in candidates:
            if fpath.exists():
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                    entry = f"\n### {rel_path}\n```\n{content}\n```"
                    if total + len(entry) > MAX_VERIFY_CHARS:
                        remain = MAX_VERIFY_CHARS - total - 100
                        if remain > 1000:
                            source_chunks.append(f"\n### {rel_path}\n```\n{content[:remain]}\n[...truncated]\n```")
                        break
                    source_chunks.append(entry)
                    total += len(entry)
                except Exception:
                    pass
                break

    if not source_chunks:
        print("[DRF] Verification skipped -no mentioned files found")
        return plan_text

    source_code = "\n".join(source_chunks)

    # critical/high 이슈만 검증 (비용 절감)
    issues_to_verify = []
    for phase in phases:
        for issue in phase.get("issues", []):
            if issue.get("severity") in ("critical", "high"):
                issues_to_verify.append({
                    "id": issue["id"],
                    "severity": issue["severity"],
                    "files": issue["files"],
                    "description": issue["description"][:300],
                })

    if not issues_to_verify:
        return plan_text

    print(f"[DRF] Verifying {len(issues_to_verify)} critical/high issues against {len(source_chunks)} files ({total} chars)...")

    prompt = VERIFY_PROMPT.format(
        issues_json=json.dumps(issues_to_verify, indent=2, ensure_ascii=False),
        source_code=source_code,
    )

    raw = _call_claude(prompt, model=claude_model)
    if not raw:
        print("[DRF] Verification failed -Claude returned empty")
        return plan_text

    verified = _extract_json(raw)
    if not verified:
        print("[DRF] Verification failed -could not parse response")
        return plan_text

    # FALSE_POSITIVE 이슈 제거
    fp_ids = set()
    if isinstance(verified, dict) and "implementation_phases" in verified:
        # 전체 plan이 반환된 경우
        for phase in verified.get("implementation_phases", []):
            for issue in phase.get("issues", []):
                if issue.get("verified") == "FALSE_POSITIVE":
                    fp_ids.add(issue.get("id", ""))
    elif isinstance(verified, list):
        # 이슈 목록만 반환된 경우
        for item in verified:
            if item.get("verified") == "FALSE_POSITIVE":
                fp_ids.add(item.get("id", ""))

    if fp_ids:
        print(f"[DRF] Removing {len(fp_ids)} false positives: {fp_ids}")
        # 원본 plan에서 false positive 제거
        for phase in phases:
            phase["issues"] = [
                iss for iss in phase.get("issues", [])
                if iss.get("id") not in fp_ids
            ]
        # total_issues 재계산
        plan_json["total_issues"] = sum(len(p.get("issues", [])) for p in phases)
        plan_json["false_positives_removed"] = list(fp_ids)

        state["messages"].append({
            "role": "verification",
            "msg": f"Removed {len(fp_ids)} false positives: {list(fp_ids)}",
            "ts": datetime.now().isoformat(),
        })

        return json.dumps(plan_json, indent=2, ensure_ascii=False)
    else:
        print("[DRF] All issues confirmed -no false positives found")
        return plan_text
