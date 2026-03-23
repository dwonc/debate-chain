# Debate Chain Changelog

---

## v8.0.0 (2026-03-23)

### 🏗️ Core Module Architecture — 전면 모듈화

debate-chain이 자기 자신을 분석해 도출한 TOP 10 개선사항을 `core/` 패키지로 구현.
기존 `server.py` 무수정, `server_patch.py`를 통해 monkey-patch 방식으로 연결.

#### #1 — `core/security.py` CLI 실행 경로 보안 강화
- `shell=True` + temp file 완전 제거 → `shell=False` + stdin 기반 `run_cli_stdin()`
- temp file 불가피 시: 보안 디렉터리 + 랜덤 파일명 + `chmod 0600` + 즉시 삭제
- API 키 / Bearer 토큰 / 이메일 / 경로 등 민감 정보 자동 redaction 파이프라인
- `load_secret()`: 환경변수 전용 비밀값 로더 (config.json에 키 저장 금지)
- 프롬프트 길이 제한 (`MAX_PROMPT_CHARS = 100,000`)

#### #2 — `core/job_store.py` Persistent Job Store + Resume
- 전역 메모리 dict → **SQLite 영속 저장** (`debate_chain.db`)
- 명시적 상태 머신: `queued → running → converged / failed / aborted`
- 서버 재시작 시 `running` 상태 → `recovering` 자동 마킹
- Optimistic locking (row version) 으로 race condition 방지
- Append-only 이벤트 로그 (`job_events` 테이블)

#### #3 — `core/provider.py` Provider 추상화 + 하이브리드 Backend
- `ProviderBackend` 추상 인터페이스 → `CLIBackend` / `SDKBackend` 분리
- CLI 우선 정체성 유지, SDK를 선택적 가속/fallback으로 활용
- `FallbackProvider`: primary 실패 시 secondary 자동 전환
- `make_claude()` / `make_codex()` / `make_gemini()` 팩토리 함수
- `ProviderResponse`: 텍스트 + 지연시간 + 토큰 수 + 에러 통합 반환

#### #4 — `core/async_worker.py` Async Worker + 우선순위 큐
- Flask 동기 처리 한계 → threading 기반 `AsyncWorkerPool`
- 우선순위 큐 (`PriorityQueue`) + 동시 4 job 실행
- 작업 취소 (`cancel(job_id)`) + job_store 상태 자동 업데이트
- daemon thread로 서버 종료 시 자동 정리

#### #5 — `core/sse.py` 실시간 SSE 스트리밍
- polling 방식 완전 대체 → pub/sub `SSEBus` 이벤트 버스
- 히스토리 리플레이: 새 구독자가 과거 이벤트 즉시 수신
- `make_sse_response(job_id)`: Flask Response 한 줄 생성
- keepalive 자동 전송 (연결 유지)

#### #6 — `core/convergence.py` 수렴 알고리즘 고도화
- 단순 평균 점수 → **4차원 복합 수렴 판정**
  - `semantic_similarity`: sentence-transformers > sklearn TF-IDF > pure Python 3-tier fallback
  - `keypoint_consensus`: 핵심 문장 추출 + 라운드 간 유지율 측정
  - `score_stability`: 최근 3라운드 점수 표준편차 기반
  - `regression_free`: 현재 점수 기반 회귀 없음 확인
- 가짜 합의 탐지: 표면 유사도 높지만 키포인트 합의 낮은 경우 수렴 거부
- 조기 수렴 방지: `min_rounds` 강제 (기본 2라운드)
- 라운드별 수렴 추이 `trends` 리스트 반환

#### #7 — `core/router.py` Task 라우팅 & 전문화
- 고정 provider 조합 → task 유형 자동 감지 후 최적 조합 선택
  - `code` → Claude(generator) + Codex+Gemini(critics)
  - `math` → Gemini(generator) + Claude+Codex(critics)
  - `creative` → Codex(generator) + Claude+Gemini(critics)
- provider 성능 이력(`ProviderHistory`) 기반 동적 라우팅 (3회 이상 데이터 시 활성화)
- `config.json`의 `routing_overrides`로 규칙 수동 오버라이드 가능

#### #8 — `web_ui/index.html` Web Dashboard
- 실시간 Job 목록 + 상태 모니터링
- 라운드별 AI 응답 (Claude / Codex / Gemini) 3단 비교
- 수렴 점수 바 + 수렴 여부 시각화
- 최종 솔루션 탭 + 클립보드 복사
- SSE 연동으로 polling 없이 실시간 업데이트
- 새 Job 제출 폼 (mode / max_rounds 선택)

#### #9 — `core/cost_tracker.py` Cost & Rate Limit 관리
- 토큰 사용량 + USD 비용 per-job JSONL 로그 (`logs/cost_log.jsonl`)
- 모델별 가격표 내장 (claude-opus, gpt-4o, gemini-2.5-flash 등)
- 세션 예산 한도 설정 (`DEBATE_BUDGET_USD` 환경변수)
- `RateLimiter`: 429 응답 시 지수 백오프 자동 재시도 (최대 4회)

#### #10 — `core/tools.py` Plugin/Tool 호출 지원
- 토론 중 AI가 외부 도구 호출 가능
  - `web_search`: DuckDuckGo API 기반 실시간 검색
  - `code_exec`: 샌드박스 Python 실행 (subprocess + timeout + 위험 패턴 차단)
  - `file_read`: 허용 경로 파일 읽기
- 프롬프트 내 `<tool:web_search>query</tool>` 태그 자동 감지 → 실행 → 결과 주입

---

### 🔧 인프라 변경

#### `server_patch.py` (신규)
- `server.py` 무수정 연결 패치
- 각 core 모듈 개별 `try/except` — 모듈 로드 실패해도 서버는 정상 기동
- 추가 라우트: `/stream/<job_id>`, `/api/jobs`, `/api/status/<id>`, `/api/system`, `/dashboard`
- 포트: 5000 (기존 server.py와 동일)

#### `start.bat` / `start_full.bat` 업데이트
- `start.bat` → `start_full.bat` 호출 단순 래퍼
- `start_full.bat`: Flask + MCP 별도 창 동시 기동, 브라우저 자동 오픈
- `.env` 자동 로드, `.venv` 자동 활성화
- 의존성 자동 설치 (flask / scikit-learn / google-generativeai)
- 인코딩 문제 해결: 박스 문자 / 한글 제거, ASCII 전용으로 통일

#### `core/types.py` (신규)
- `TaskType`, `RouteResult`, `ProviderStats`, `ToolResult` 공유 타입 정의

#### `core/__init__.py` (신규)
- 10개 모듈 전체 re-export

#### `requirements.txt` 업데이트
- `scikit-learn`, `duckduckgo-search` 추가
- `sentence-transformers`, `anthropic`, `openai` 선택적 주석 처리

---

### 📁 신규 파일 목록
```
core/__init__.py
core/security.py
core/job_store.py
core/provider.py
core/async_worker.py
core/sse.py
core/convergence.py
core/router.py
core/cost_tracker.py
core/tools.py
core/types.py
web_ui/index.html
server_patch.py
start_full.bat
```

---

## v7.0.0 (2026-03-23)

### 🏗️ Architecture — 엔진 전면 재설계

#### Phase 1: subprocess 보안 & 속도 최적화
- `shell=True` + temp file 방식 → `shell=False` + stdin 직접 전달로 전환
  - 보안 취약점(shell injection) 완전 제거
  - 호출당 ~500ms 오버헤드 절약
  - Codex/Gemini stdin 미지원 시 cross-platform temp file fallback 유지
- Windows npm 절대경로 하드코딩 → `platform` + `shutil.which` 기반 OS 자동 감지로 교체
  - Windows: `cmd /c %APPDATA%\npm\claude.cmd`
  - Mac/Linux: `shutil.which("claude")` PATH 자동 탐색
  - **Mac/Linux 완전 호환** 달성

#### Phase 2: Multi-Critic + 품질 고도화
- **Multi-Critic 도입**: Codex + Gemini를 `ThreadPoolExecutor`로 병렬 실행
  - 두 Critic 중 낮은 점수를 수렴 기준으로 채택 (보수적 품질 보증)
  - 이슈 중복 제거 후 합산, 출처(source) 태깅
- **Synthesizer 모델 분리**: Generator(Claude) → Synthesizer(Codex)
  - 자기 코드 자기 수정 편향 제거
  - 독립적 시각에서 개선안 생성
- **Regression Detection**: 이전 라운드에서 수정된 이슈 재발 감지
  - `previously_fixed` 누적 전달, Critic이 회귀 여부 명시 판정
- **다차원 수렴 판정**: overall 점수 단일 기준 → 4개 차원 개별 검증
  - `correctness`, `completeness`, `security`, `performance` 각각 min 6.0 이상
  - critical 이슈 잔존 시 수렴 불가, regression 감지 시 수렴 불가

#### Phase 3: 파이프라인 & 스트리밍
- **`debate_pair` 파이프라인**: debate 완료 → 구조화된 아티팩트 추출 → pair 자동 연결
- **`self_improve` 루프**: 반복 자기개선 + 최종 Codex Critic 검증
- **SSE 스트리밍**: `/api/stream/<job_id>` — 폴링 없이 실시간 상태 push

---

### 🔧 API 변경

| 엔드포인트 | 설명 |
|---|---|
| `GET /api/result/<id>` | debate full result |
| `GET /api/pair/result/<id>` | pair full result |
| `GET /api/debate_pair` | debate→pair 파이프라인 |
| `POST /api/self_improve` | 자기개선 루프 |
| `GET /api/stream/<id>` | SSE 실시간 스트리밍 |

---

### 🐛 버그 수정
- MCP timeout/fail 근본 원인 해결: status compact 분리
- subprocess timeout 300s → 600s

---

## v6.x 이전

- 초기 debate 엔진 구현 (Claude Generator + Gemini Critic 단일)
- pair 병렬 생성 모드 추가
- Flask 웹 UI 구현
- MCP 서버 연동 (7개 개별 도구)
