"""
core/prompts.py — R17: All prompt templates extracted from server.py

모든 프롬프트 상수는 이 파일에서 관리.
변경 시 서버 재시작 필�� (향후 YAML/hot-reload 전환 고려).
"""

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
