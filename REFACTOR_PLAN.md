# Horcrux v9 리팩토링 기획안

> **관점**: 호크룩스가 스스로를 호크룩스에 넣고 돌렸을 때 나올 분석
> **날짜**: 2026-04-04
> **현재 버전**: v8.3 (14,252 lines / 40+ files)

---

## 0. 자기진단 — "내가 나를 본다면"

호크룩스는 "Generator ≠ Critic ≠ Synthesizer"라는 구조적 편향 제거를 핵심 철학으로 삼는다.
그런데 정작 **호크룩스 자신의 코드에는 이 원칙이 적용되지 않았다.**

- `server.py` 하나가 Generator(AI 호출) + Critic(점수 로직) + Synthesizer(결과 조합) + Router(라우팅) + UI(HTML 템플릿)를 전부 담당
- 역할 분리 없는 2,911줄짜리 God Object — 호크룩스가 자신을 분석하면 "critical issue: SRP 위반" 판정

---

## 1. 아키텍처 관점 — 구조적 부채

### 1-1. God Object: `server.py` (2,911 lines)

| 현재 server.py가 담당하는 역할 | 줄 수 (추정) |
|------|------|
| Flask 앱 + 라우팅 + CORS | ~200 |
| AI 호출 함수 (call_claude, call_codex, call_gemini, call_aux_*) | ~600 |
| HTML 템플릿 (UI 전체) | ~500 |
| 멀티크리틱 + 점수 계산 + 수렴 판정 | ~400 |
| 디베이트 루프 (run_debate_loop) | ~300 |
| 상태 관리 + 로깅 + SSE | ~300 |
| Pair 모드 로직 | ~200 |
| 기타 유틸 (JSON 추출, 프롬프트 자르기 등) | ~400 |

**진단**: 한 파일이 8개 역할을 담당 → 변경 영향 범위 예측 불가, 테스트 불가능

### 1-2. 레거시 이중 구조

| 파일 | 상태 | 비고 |
|------|------|------|
| `orchestrator.py` (382줄) | 레거시 v5 | adaptive가 대체했지만 debate_loop 폴백으로 남아있음 |
| `planning.py` (462줄) | 레거시 v1 | planning_v2가 완전 대체 |
| `mcp_server.py` | 레거시 | mcp_server.js가 대체 |
| `core/convergence.py` (351줄) | 반 레거시 | TF-IDF 유사도 — 현재 사용처 불명확 |
| `core/timeout_budget.py` (23줄) | 중복 | `core/adaptive/timeout_budget.py` (299줄)와 역할 겹침 |

**진단**: 코드의 ~15%가 죽은 코드 또는 중복 → 인지 부하 증가, 실수 유발

### 1-3. 순환 의존 & Self-HTTP 호출

```
server.py → planning_v2.py → (server.py의 call_claude 등을 inject)
server.py → deep_refactor.py → (server.py의 call_claude 등을 inject)
server.py → adaptive_orchestrator.py → (requests.post("http://localhost:5000/..."))
```

- `adaptive_orchestrator.py`가 자기 서버에 HTTP 요청 → 불필요한 네트워크 오버헤드, 서버 미기동 시 실패
- 함수 주입(inject) 패턴이 DI가 아닌 런타임 monkey-patching

---

## 2. 코드 품질 관점 — 호크룩스 Critic이 매기는 점수

호크룩스의 자체 채점 기준으로 평가:

| 차원 | 점수 | 근거 |
|------|------|------|
| **Correctness** | 7/10 | 동작은 하나, edge case 처리가 try/except 남발 |
| **Completeness** | 6/10 | 기능은 풍부하나 테스트 커버리지 ~5% |
| **Security** | 4/10 | 인증 없음, API키 .env 의존, CORS 무방비 |
| **Performance** | 5/10 | 캐시 없음, 매번 fresh LLM 호출, 동기 blocking |
| **Maintainability** | 3/10 | 2,911줄 God Object, 레거시 혼재, 타입힌트 부분적 |
| **종합** | 5.0/10 | **수렴 실패 — 리비전 필요** |

> 호크룩스 기준 수렴 조건: overall ≥ 8.0, 모든 차원 ≥ 6.0
> → **현재 3개 차원이 6.0 미만** → 수렴 불가 판정

---

## 3. 관점별 분석 & 리팩토링 포인트

### 관점 A: 사용자(User) 입장

| 문제 | 영향 | 개선 방향 |
|------|------|------|
| UI가 server.py 안에 HTML 문자열로 존재 | 수정 시 파이썬 서버 전체 재시작 | 정적 HTML 분리 (web_ui/ 이미 존재하나 미활용) |
| 에러 시 500 + 긴 traceback | 사용자 혼란 | 구조화된 에러 응답 (error code + message) |
| 진행 상태가 SSE + 폴링 혼재 | 일관성 없음 | WebSocket 또는 SSE 단일화 |
| 결과 품질 편차 큼 (fast 5.0 vs full 7.5) | 기대치 불일치 | fast 모드 최소 품질 보장 또는 경고 표시 |

### 관점 B: 개발자(Contributor) 입장

| 문제 | 영향 | 개선 방향 |
|------|------|------|
| server.py 하나 수정하면 전체 영향 | PR 리뷰 불가능 | 모듈 분리 |
| 테스트 1개 파일 (test_phase1.py) | 리팩토링 공포 | 핵심 로직 단위 테스트 추가 |
| 함수 inject 패턴 | 디버깅 어려움 | 명시적 DI 컨테이너 또는 Protocol |
| 한국어/영어 혼재 주석 | 일관성 없음 | 주석 언어 통일 |

### 관점 C: 운영(Ops) 입장

| 문제 | 영향 | 개선 방향 |
|------|------|------|
| 인증/인가 없음 | localhost 전제 깨지면 API 노출 | API key 기반 인증 최소 추가 |
| Rate limiting 없음 | 외부 API 비용 폭주 가능 | 요청별/분별 제한 |
| 로그가 JSONL 파일 기반 | 검색 어려움, 디스크 증가 | 로그 로테이션 + 구조화 (최소 rotate) |
| SQLite 단일 파일 | 동시성 한계 | 현재 규모에서는 OK, 스케일 시 PostgreSQL |
| 배포 스크립트 없음 | start.bat만 존재 | Docker 또는 systemd 지원 |

### 관점 D: 비용(Cost) 입장

| 문제 | 영향 | 개선 방향 |
|------|------|------|
| 동일 태스크 재실행 시 캐시 없음 | 불필요한 API 비용 | 프롬프트 해시 기반 캐시 (TTL) |
| Aux critic 항상 3개 실행 | fast에서도 비용 발생 | conditional_aux 실제 적용 확인 |
| 프롬프트 60K 고정 truncate | 토큰 낭비 | 태스크 복잡도별 동적 컨텍스트 크기 |
| cost_tracker 존재하나 활용 안 됨 | 비용 가시성 없음 | 세션별 비용 표시 + 예산 한도 설정 |

### 관점 E: 확장성(Scale) 입장

| 문제 | 영향 | 개선 방향 |
|------|------|------|
| 단일 Flask 프로세스 | 동시 요청 1개 (GIL + sync) | Gunicorn + async worker 또는 FastAPI |
| ThreadPoolExecutor 하드코딩 | 워커 수 조절 불가 | 설정 기반 풀 사이즈 |
| 상태가 in-memory dict + SQLite | 멀티프로세스 불가 | Redis 또는 DB 기반 상태 관리 |

---

## 4. 리팩토링 우선순위 — Critical Path

### Phase 1: 구조 분리 (Maintainability 3→6)

> **목표**: server.py 2,911줄 → 각 300줄 이하 모듈로 분리

```
server.py (2911줄)
    ↓ 분리
├─ server.py (~300줄)              # Flask app + 라우팅만
├─ core/llm/
│   ├─ claude.py                   # call_claude + 프롬프트 관리
│   ├─ codex.py                    # call_codex + fallback chain
│   ├─ gemini.py                   # call_gemini + 모델 로테이션
│   └─ aux.py                      # call_aux_critics (Groq/DeepSeek/OpenRouter)
├─ core/debate/
│   ├─ engine.py                   # run_debate_loop
│   ├─ scoring.py                  # multi_critic + 가중 평균 + 수렴 판정
│   └─ prompts.py                  # 모든 프롬프트 템플릿
├─ core/api/
│   ├─ horcrux_routes.py           # /api/horcrux/* 엔드포인트
│   ├─ legacy_routes.py            # /api/start, /api/pair 등
│   └─ analytics_routes.py         # /api/analytics/*
├─ core/util/
│   ├─ json_extract.py             # extract_json, safe_parse
│   └─ prompt_util.py              # truncate, merge
└─ web_ui/
    ├─ index.html                  # (이미 존재 - 활용)
    └─ planning.html               # (이미 존재)
```

**예상 효과**:
- 파일당 최대 300줄 → 단일 책임
- 각 모듈 독립 테스트 가능
- LLM 호출 함수 재사용 (planning_v2, deep_refactor에서 inject 불필요)

### Phase 2: 레거시 정리 (Completeness 6→7)

| 작업 | 파일 | 조치 |
|------|------|------|
| v1 planning 제거 | `planning.py` (462줄) | 삭제 (planning_v2로 완전 대체) |
| v5 orchestrator 정리 | `orchestrator.py` (382줄) | debate_loop만 추출 → core/debate/engine.py |
| Python MCP 제거 | `mcp_server.py` | 삭제 (JS MCP로 대체 완료) |
| 중복 timeout 제거 | `core/timeout_budget.py` (23줄) | 삭제 (adaptive/ 버전 유지) |
| convergence 평가 | `core/convergence.py` (351줄) | 사용처 확인 → 미사용 시 삭제 |
| self-HTTP 제거 | `adaptive_orchestrator.py` | requests.post(localhost) → 직접 함수 호출 |

**예상 효과**: ~1,600줄 제거, 인지 부하 40% 감소

### Phase 3: 테스트 인프라 (Correctness 7→8)

```
tests/
├─ test_classifier.py        # 분류기 유닛 테스트 (기존 확장)
├─ test_scoring.py           # 점수 계산 + 수렴 판정 테스트
├─ test_llm_mock.py          # LLM 호출 mock + 응답 파싱 테스트
├─ test_debate_engine.py     # 디베이트 루프 시나리오 테스트
├─ test_api_integration.py   # API 엔드포인트 통합 테스트
└─ conftest.py               # 공통 fixture (mock LLM, test config)
```

**핵심**: LLM 호출은 mock, 오케스트레이션 로직만 테스트

### Phase 4: 보안 & 운영 (Security 4→7)

| 작업 | 방법 |
|------|------|
| API 인증 | Bearer token 또는 API key 헤더 (config.json에 설정) |
| Rate limiting | Flask-Limiter (분당 10회 등) |
| CORS 설정 | 허용 origin 명시적 지정 |
| 로그 로테이션 | RotatingFileHandler (100MB, 5 backup) |
| 비용 대시보드 | cost_tracker 활성화 + UI 표시 |

### Phase 5: 성능 & DX (Performance 5→7)

| 작업 | 방법 |
|------|------|
| 프롬프트 캐시 | SHA256 해시 → 로컬 캐시 (TTL 1h) |
| Flask → FastAPI 검토 | async/await 네이티브, 자동 API 문서 |
| UI 분리 | server.py에서 HTML 제거 → web_ui/ 정적 서빙 |
| Hot reload | 개발 시 UI 변경 서버 재시작 불필요 |

---

## 5. 리팩토링하지 않을 것 — 과잉 엔지니어링 방지

호크룩스의 실험 결과가 말해주듯, **오케스트레이션 깊이 > 인프라 품질**.
아래는 현 단계에서 불필요한 작업:

| 하지 않을 것 | 이유 |
|------|------|
| 마이크로서비스 분리 | 단일 사용자 로컬 도구 — 오버킬 |
| PostgreSQL 전환 | SQLite가 현재 규모에 충분 |
| Kubernetes/Docker 배포 | start.bat → python server.py로 충분 |
| GraphQL API | REST가 이 규모에 적합 |
| React/Vue 프론트엔드 | 간단한 HTML+JS로 충분 |
| 인증 서버 (OAuth/OIDC) | 로컬 도구에 불필요 |

---

## 6. 실행 로드맵

```
Phase 1 (구조 분리)     ████████████████ — 핵심, 나머지의 전제 조건
Phase 2 (레거시 정리)   ████████         — Phase 1과 병렬 가능
Phase 3 (테스트)        ████████████     — Phase 1 완료 후
Phase 4 (보안/운영)     ████████         — 독립적, 언제든 가능
Phase 5 (성능/DX)       ████████████     — 마지막 (있으면 좋고)
```

**Phase 1이 모든 것의 기반** — server.py 분리 없이는 테스트도, 레거시 정리도 어려움

---

## 7. 자기참조 메타 분석 — "이 기획안을 호크룩스에 넣으면?"

이 기획안 자체를 `/api/horcrux/run`에 넣고 full 모드로 돌리면:

- **Generator** (Sonnet): 분리 구조 제안
- **Critic** (Codex): "Phase 1 범위가 너무 넓다. 더 작은 단위로 쪼개라"
- **Aux Critics**: "Flask→FastAPI 전환은 Phase 5가 아니라 Phase 1에 해야 한다" (Groq) vs "현 단계에서 불필요" (DeepSeek)
- **Synthesizer**: Phase 1을 sub-phase로 분할 (1a: LLM 분리, 1b: 라우트 분리, 1c: UI 분리)
- **예상 점수**: 7.5/10 → "Phase 1 세분화 후 8.0 달성 가능"

---

## 8. 다음 단계

이 기획안을 기반으로:
1. **Phase 1a** 먼저 착수 — `server.py`에서 LLM 호출 함수만 `core/llm/`로 추출
2. 기존 `inject_callers` 패턴을 `from core.llm import call_claude` 직접 import로 전환
3. 동작 확인 후 Phase 1b (라우트 분리) 진행

> **"오케스트레이션이 모델보다 중요하다"** — 호크룩스 v8.3 실험 결론
> 마찬가지로, **코드 구조가 기능 추가보다 중요하다** — v9의 출발점
