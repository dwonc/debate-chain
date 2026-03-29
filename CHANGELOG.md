# Horcrux Changelog

## v8.3.0 — 2026-03-29

### 실험 기반 최적화 — 비용 90% 절감, 품질 동등

24회 실행 실험(Opus vs Sonnet x standard/full x 3난이도)으로 최적 조합 도출.

#### 핵심 실험 결과
| 조합 | 평균 점수 | 비용/회 |
|------|----------|--------|
| Opus+standard | 6.5 | ~$0.40 |
| Opus+full | 7.7 | ~$0.80 |
| Sonnet+standard | 4.8 | ~$0.04 |
| **Sonnet+full** | **7.5** | **~$0.08** |

- Sonnet+full(7.5) = Opus+full(7.7) at 1/10 cost
- Sonnet+full(7.5) > Opus+standard(6.5) — 오케스트레이션 > 모델 성능
- Medium에서 Sonnet+full(8.5)이 Opus+full(7.5) 역전

#### 코드 반영
- **기본 모델 변경**: Opus -> **Sonnet** (`call_claude` default)
- **기본 라우팅 변경**: auto 모드에서 standard -> **full** (`_route_intent_to_engine`)
  - fast: `code_fix + small + low_risk`만
  - 나머지 전부: full
- **Sonnet 보정**: `apply_sonnet_compensation()` — hard task 시 full 자동 승격
- **`.horcrux/optimization.md`**: 실험 데이터 기반 라우팅 정책 문서

#### 버그 수정 (이번 세션)
- `AuxDecision.should_run` -> `run_aux` 필드명 오타 수정
- Codex CLI `-q` 플래그 제거 (CLI 업데이트 대응) -> `exec` 서브커맨드
- Windows cp949 인코딩 크래시 -> `chcp 65001` + `PYTHONIOENCODING=utf-8`
- generator timeout 30초 -> 120초 (한국어/복잡 태스크 대응)
- mode override 시 intent가 모드를 덮어쓰던 문제 수정
- 404 폴링 무한 반복 -> 비정상 응답 시 폴링 중단
- 빈/깨진 로그 파일 JSON 파싱 에러 -> graceful 404 반환
- 동기 `hrx_` 스레드 로그 파일 미저장 -> 서버 재시작 후에도 유지

#### 실험 인프라
- `run_experiment_mini.py`: 간편 실험 (3태스크 x 2모델)
- `run_experiment_phase2.py`: full 모드 + 크로스 비교
- `experiment_results/`: 전체 실험 데이터 + 한글 리포트

---

## v8.2.0 — 2026-03-28

### Deep Refactor 모드 — 멀티모델 코드 리팩토링 분석 엔진

코드 생성이 아닌 **기존 코드의 리팩토링 분석**에 특화된 새 엔진. 3개 모델이 각각 독립적으로 전체 소스코드를 분석하고, 5개 모델이 크리틱-리비전 루프로 검증.

#### 신규 파일
| 파일 | 내용 |
|------|------|
| `deep_refactor.py` | 5-phase 파이프라인: auto-split → 병렬 분석 → 종합 → 크리틱 루프 → 결과 |

#### 핵심 기능

**Auto-Split (Phase 0)**
- 프로젝트 file tree를 Claude가 분석하여 모듈 그룹으로 자동 분할
- 작은 프로젝트(≤50K)는 분할 없이 단일 그룹으로 처리
- Claude 분할 실패 시 디렉토리 기반 fallback 분할
- 최대 6그룹, 그룹당 50K chars

**병렬 분석 (Phase 1)**
- 그룹별 × 3모델(Claude/Codex/Gemini) = 최대 18개 동시 분석
- 각 모델이 다른 관점에서 분석: 아키텍처 / 코드 품질 / 유지보수성
- 기존 8K 제한 → 그룹당 50K, 전체 프로젝트 100% 커버

**크리틱-리비전 루프 (Phase 3)**
- 5개 모델(Claude + Codex + Gemini + Groq + DeepSeek) 병렬 크리틱
- 수렴까지 최대 3라운드 반복

#### server.py 변경
- `deep_refactor.py` import 및 의존성 주입 (`inject_drf_callers`)
- `/api/horcrux/run`에 `deep_refactor` 엔진 라우팅 추가
- `horcrux_status`, `horcrux_result`에서 `drf_` prefix 인식
- Web UI `statusUrl`에 `drf_` prefix 추가

#### classifier.py 변경
- `InternalEngine.DEEP_REFACTOR` 추가
- `DetectedIntent.DEEP_REFACTOR` 추가
- `mode="deep_refactor"` override 시 직접 라우팅

#### start.bat 변경
- 모드 표시에 "Deep Refactor" 추가

---

### Pair 모드 Architect 프롬프트 강화

Parallel(pair) 모드에서 두 파트 간 인터페이스 불일치 문제 해결을 위해 architect 프롬프트를 대폭 강화.

#### SPLIT_PROMPT 변경
- `shared_spec`에 4개 필드 요구: `interfaces`(클래스/함수 시그니처 + 타입), `imports`(정확한 import문), `conventions`(config 패턴, 네이밍), `shared_files`(파일 소유권)
- 각 part에 `owns` 필드 추가 — 담당 파일 명시

#### PART_PROMPT 변경
- shared spec 준수 룰 5개 추가
- 클래스명, 시그니처, import 패턴 변경 금지 명시
- 자기 파트 파일만 작성하도록 제한

---

### Analytics 전면 확장 — 모든 로그 타입 집계

기존에 `*_result.json` 패턴(adaptive 모드)만 읽던 analytics를 모든 로그 타입으로 확장.

#### analytics.py 변경
- `compute_critic_reliability()`: `*.json` 전체 스캔. debate(`messages[].score` vs `avg_score`), adaptive(`history[].score` vs `final_score`) 모두 집계
- `compute_mode_usage_stats()`: `*.json` 전체 스캔. `_infer_mode()` 추가 — id prefix, status 등에서 모드 자동 추론
- `_guess_critic_model()` 헬퍼 추가 — critic 텍스트에서 모델명 추정
- `datetime` import 추가 (버그 수정)
- 결과: 기존 3/10 리뷰 → **417개 리뷰** 집계

#### Scoring 가중치 자동 튜닝
- `server.py`에 `_maybe_auto_tune_scoring()` 추가
- debate, pair, self_improve, adaptive 4곳 완료 시 호출
- 10회 완료마다 `auto_tune_scoring_weights(dry_run=False)` 실행 → `config.json` 자동 반영
- 서버 콘솔에 `[AUTO-TUNE]` 로그 출력

#### 적용 결과
- `core_weight`: 0.8 → **0.6** (critic 데이터 기반 자동 산출)
- `aux_weight`: 0.2 → **0.4**

---

## v8.0.0 — 2026-03-27

### Adaptive Single Entry Point — 외부 인터페이스 통합 리팩토링

외부 모드 8개(debate, planning, pair2, pair3, debate_pair2, debate_pair3, adaptive, self_improve)를 5개(Auto, Fast, Standard, Full, Parallel)로 통합. 내부 코어 엔진은 그대로 유지하되, 사용자에게는 `/api/horcrux/run` 하나만 노출.

### classifier.py 확장
- `InternalEngine` enum: adaptive_fast, adaptive_standard, adaptive_full, debate_loop, planning_pipeline, pair_generation, self_improve
- `DetectedIntent` enum: code_fix, feature_add, refactor, brainstorm, document, artifact, parallel_gen, self_improve
- `_detect_intent()` — 키워드 기반 intent 감지 (LLM 호출 없음, 즉시 반환)
- `_route_intent_to_engine()` — intent + mode → (final_mode, internal_engine) 매핑
- `ClassificationResult`에 `internal_engine`, `detected_intent` 필드 추가
- `HorcruxMode`에 `FULL`, `PARALLEL` 추가

### server.py — 통합 엔드포인트
- `POST /api/horcrux/run` — classify → engine 결정 → 해당 엔진 호출
  - 동기 엔진(adaptive_*): solution 직접 반환
  - 비동기 엔진(planning/pair/debate/self_improve): job_id 반환 → check로 폴링
- `POST /api/horcrux/classify` — 분류 미리보기
- `GET /api/horcrux/status/{id}`, `GET /api/horcrux/result/{id}`, `POST /api/horcrux/stop/{id}`
- `/api/adaptive/*` 6개 엔드포인트 **제거** (horcrux로 대체)

### mcp_server.js v8.0
- 도구 5개로 축소: `run`, `check`, `classify`, `analytics`, `horcrux_test`
- `run` description에 "코드 수정, 브레인스토밍, 문서 작성, PPT 생성, 아키텍처 설계" 명시 → Claude Desktop이 자연어에서 자동 라우팅
- 레거시 도구 7개 제거: debate_start/status/result, pair2_start, pair3_start, pair_status/result, self_improve

### HTML_TEMPLATE 변경
- 모드 버튼: Debate/Planning/Adaptive/Pair2/Pair3 → **Auto/Fast/Standard/Full/Parallel**
- 옵션 패널: Auto(scope/risk/artifact), Full(artifact/audience/tone), Parallel(parts/output_dir)
- `startDebate()` → `startRun()`: 모든 모드 → `/api/horcrux/run` 하나로 호출
- 동기 응답 시 즉시 렌더링, 비동기 응답 시 기존 polling 유지
- 기존 `adp_` prefix 스레드 하위 호환 유지

### 삭제된 파일
- `mcp_adaptive_ext.py` — JS MCP로 통합됨
- `mcp_server.py`에서 adaptive 핸들러 참조 제거

### 마이그레이션 가이드
- MCP 설정: `python mcp_server.py` → `node mcp_server.js` 변경 필수
- Web UI: 서버 재시작만 하면 자동 반영
- 기존 스레드: `adp_` prefix 자동 호환, 데이터 유실 없음

---

## v6.0.0 — 2026-03-27

### 프로젝트 리브랜딩: Debate Chain → Horcrux

전체 프로젝트명을 `debate-chain`에서 `Horcrux`로 변경. 폴더명, 코드 내 참조, 패키지명, DB 파일명, .gitignore 등 28개 파일 62+개 참조 일괄 교체.

### Phase 1.5: Compact State Memory + Delta Prompting

전체 이전 출력을 매번 재삽입하던 구조에서, 압축된 상태 메모리와 delta만 전달하는 구조로 전환.

**1. compact_memory.py — 3-Layer 메모리 시스템**
- `CompactMemory` — 모드별 차등 메모리 관리자
- `WorkingMemory` — task, goal, blockers, preserve (모든 모드)
- `DecisionMemory` — accepted/rejected decisions, open questions (standard, full_horcrux)
- `ResultSummaryMemory` — content/structure summary, resolved items (standard, full_horcrux)
- Mode policy: fast=working only, standard=working+result, full_horcrux=all 3

**2. RoundCheckpoint — 라운드별 체크포인트**
- 매 라운드 후 score, blockers, preserve, conclusion 저장
- 다음 라운드에 full output 대신 checkpoint만 입력

**3. DeltaPrompt — 변경분만 전달**
- `build_revision_prompt()` — new_blockers + resolved_items + preserve만 전달
- `build_critic_prompt()` — checkpoint 기반 context
- Hard rules: previous full output 재삽입 금지, critic 전체 목록 재삽입 금지

**적용 범위:**
- `adaptive_orchestrator.py` — fast, standard 모드 모두 적용
- standard: compact memory + checkpoint + delta prompting
- fast: working_memory + delta revision prompt

### Phase 2: Writer/Patch/Artifact 안정화 + Aux 고도화

**1. writer_lock.py — Single Writer Rule**
- `WriterLock` — 한 번에 1 agent만 write 가능
- Role-based access: generators(read+propose), critics(read+comment), director(select+merge), writer(write+test)
- `acquire_write()` / `release_write()` — thread-safe lock

**2. patch_format.py — JSON Patch 표준화**
- `PatchSet` / `FilePatch` / `PatchHunk` — 구조화된 patch 포맷
- `parse_patch_from_llm_output()` — LLM 출력에서 JSON patch 파싱 (3단계 전략)
- `merge_patch_sets()` — 여러 agent patch merge (겹치는 hunk 첫 번째 우선)
- `PATCH_PROPOSAL_PROMPT_SUFFIX` — LLM에게 JSON patch 응답 유도

**3. conditional_aux.py — 조건부 Aux Critic**
- `should_run_aux_critics()` — mode/score/risk 기반 활성화 판정
- Skip: fast mode, low-risk + clear convergence
- Activate: full_horcrux, high-risk, core disagreement(≥2.0), threshold 근처(±1.0)

**4. artifact_spec.py — Artifact Spec 최적화**
- `ArtifactSpec` / `SlideSpec` / `DocSection` — spec 기반 렌더링 구조
- `build_artifact_spec_prompt()` — content → spec 변환 프롬프트
- `build_artifact_critic_prompt()` — 정보량/흐름/누락만 점검 (내용 재해석 금지)

**5. fallback_chain.py — 고급 Fallback Chain**
- `execute_fallback_chain()` — stage별 단계적 fallback 실행
- generator: retry_once → use_partial_results → fallback_model
- synth: use_best_candidate → retry_with_shorter_prompt
- core_critic: retry_once → fallback_critic_model → flag_unresolved
- aux_critic: skip_immediately
- revision: keep_current_version → flag_blockers

### Phase 3: 운영 데이터 기반 튜닝 + 자동 최적화

**1. analytics.py — Timeout Auto-Tuning**
- `compute_latency_percentiles()` — jsonl 로그에서 stage별 P50/P90/P99 산출
- `auto_tune_timeouts()` — 1.75x P90 기반 timeout 추천 (dry_run/apply)

**2. analytics.py — Routing Heuristic 미세 조정**
- `compute_mode_usage_stats()` — 모드별 usage/score/latency/convergence 통계
- `suggest_heuristic_refinements()` — 키워드/threshold 조정 추천

**3. classifier.py — LLM Fallback 분류 구현**
- `_llm_classify_fallback()` — confidence < 0.6 시 Claude에 10-token 분류 prompt
- `build_llm_classify_prompt()` / `parse_llm_classify_response()` — 프롬프트/파싱

**4. analytics.py — Critic Reliability Weighting**
- `compute_critic_reliability()` — critic별 score vs final_score 차이 + 분산 추적
- reliability_score 0~1, recommended_weight 0.5~1.5

**5. analytics.py — Mode Analytics Dashboard**
- `build_analytics_dashboard()` — 전체 메트릭 통합
- API: `/api/analytics`, `/api/analytics/timeouts`, `/api/analytics/critics`, `/api/analytics/modes`, `/api/analytics/heuristic`

### UI 변경

**모드 버튼 5개 추가**
- Debate (cyan) / Planning (purple) / Adaptive (blue-purple) / Pair2 (green) / Pair3 (yellow)
- 각 모드별 전용 옵션 UI, progress 표시, 결과 렌더링

**Adaptive 옵션 UI**
- Mode (auto/fast/standard/full_horcrux), Scope, Risk, Files, Task Type, Artifact Type

**Pair 모드 UI**
- Architect → Parallel Generation 구분 렌더링
- parts 완료 수 progress bar

### urgent_fix 처리
- `adaptive_routes.py` 삭제 — 메인 UI에 통합
- `run_server.py` 삭제 — server.py 직접 사용
- `start.bat` — `%~dp0` 상대경로, Horcrux 네이밍

### 신규 파일

| 파일 | 내용 |
|------|------|
| core/adaptive/compact_memory.py | Phase 1.5 — 3-layer memory + checkpoint + delta |
| core/adaptive/writer_lock.py | Phase 2 — single writer rule |
| core/adaptive/patch_format.py | Phase 2 — JSON patch 표준화 |
| core/adaptive/conditional_aux.py | Phase 2 — 조건부 aux 활성화 |
| core/adaptive/artifact_spec.py | Phase 2 — artifact spec 렌더링 |
| core/adaptive/fallback_chain.py | Phase 2 — 고급 fallback chain |
| core/adaptive/analytics.py | Phase 3 — 전체 analytics |

### 수정 파일

| 파일 | 내용 |
|------|------|
| server.py | Adaptive/Pair2/Pair3 모드 UI + API routes + Analytics routes |
| adaptive_orchestrator.py | CompactMemory 적용 (fast, standard) |
| core/adaptive/classifier.py | LLM fallback 실제 구현 |
| core/adaptive/__init__.py | Phase 1.5~3 전체 export |
| start.bat | Horcrux 네이밍 + %~dp0 경로 |

---

## v5.3.0 — 2026-03-27

### Phase 1: Adaptive Horcrux — 난이도 기반 3-tier 모드 라우팅

모든 작업에 풀체인을 태우던 구조에서, 난이도/위험도에 따라 Fast / Standard / Full Horcrux로 자동 분기.

- `classify_task_complexity()` — heuristic-first 라우팅
- `build_stage_plan()` — mode별 실행 stage 자동 구성
- `should_continue_revision()` — revision hard cap 2회
- `run_with_timeout_budget()` — stage별 timeout + latency logging
- `run_adaptive()` — 새 메인 진입점
- 30개 테스트 전체 통과

---

## v5.2.0 — 2026-03-26

### Layer 3 Planning Pipeline

- planning_v2.py — content_profile + artifact_profile 통합 하네스
- task_type 라우팅: brainstorm/portfolio/hybrid/artifact_only
- artifact spec builder + critic + renderer

---

## v5.1.0 — 2026-03-25

### 5-Model Parallel Critic

- Aux Critics: Groq/Llama 3.3 + DeepSeek V3 + GPT-OSS 120B
- 점수: Core min × 0.8 + Aux avg × 0.2
- Claude 모델 스위칭 (Opus ↔ Sonnet)
- Codex fallback chain (CLI → OpenAI SDK → OSS)

---

## v8.1.0 — 2026-03-23
- Codex exec stdin 복원, Claude cwd=tempdir
- pair output_dir / project_dir

## v8.0.0 — 2026-03-23
- core/ 모듈 분리, server_patch.py

## v7.0.0
- Multi-Critic (Codex+Gemini), min() 점수
- Synthesizer=Codex, 다차원 수렴, Regression detection
- pair2/pair3, debate_pair, self_improve
- MCP, SSE 스트리밍
