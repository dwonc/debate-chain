# Debate Chain Changelog

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
  - `extract_debate_artifact()`: key_decisions, resolved_issues, remaining_concerns 구조화
  - Pair splitter가 debate 결과를 컨텍스트로 활용
- **`self_improve` 루프**: 반복 자기개선 + 최종 Codex Critic 검증
  - 기존 debate `final_solution`을 시드로 이어받기 가능
- **SSE 스트리밍**: `/api/stream/<job_id>` — 폴링 없이 실시간 상태 push

---

### 🔧 API 변경

#### 신규 엔드포인트
| 엔드포인트 | 설명 |
|---|---|
| `GET /api/result/<id>` | debate full result (완료 시만) |
| `GET /api/pair/result/<id>` | pair full result |
| `GET /api/debate_pair` | debate→pair 파이프라인 시작 |
| `GET /api/pipeline/status/<id>` | 파이프라인 상태 |
| `GET /api/pipeline/result/<id>` | 파이프라인 결과 |
| `POST /api/self_improve` | 자기개선 루프 시작 |
| `GET /api/self_improve/status/<id>` | self_improve 상태 |
| `GET /api/self_improve/result/<id>` | self_improve 결과 |
| `GET /api/stream/<id>` | SSE 실시간 스트리밍 |

#### 기존 엔드포인트 변경
- `GET /api/status/<id>` — **compact 전용**: messages 배열 제거, 메타데이터만 반환
  - 폴링 payload 폭발 문제 근본 해결 (MCP timeout/fail의 핵심 원인)
- `POST /api/start` — `parent_debate_id` 파라미터 추가 (Deep Dive용)

---

### 🔍 Deep Dive 기능
- debate 완료 후 결과가 마음에 안 들면 `final_solution`을 시드로 새 debate 연속 실행
- **UI**: 완료된 debate에 `🔍 Deep Dive` 버튼 표시
  - 클릭 시 포커스 힌트 입력창 (선택사항, 스킵 가능)
  - 이전 결과를 `initial_solution`으로 자동 전달
- **MCP**: `run(task="...", parent_debate_id="20260321_155812")`
- **계보 추적**: 결과 하단에 `(child of <parent_id>)` 표시

---

### 🖥️ MCP 도구 정리 (mcp_server.js v7.0)

#### 신규 통합 도구
- **`run`**: debate / pair2 / pair3 / debate_pair2 / debate_pair3 통합 실행
  - `parent_debate_id` 파라미터 추가 (Deep Dive 체이닝)
- **`check`**: 모든 job 타입(debate/pair/pipeline/self_improve) 통합 상태 확인
  - 실행 중: compact 메타데이터만 반환
  - 완료 시: full result 자동 포함

#### 레거시 도구 (하위 호환 유지)
`debate_start`, `debate_status`, `debate_result`, `pair2_start`, `pair3_start`, `pair_status`, `pair_result`

---

### 🐛 버그 수정
- **MCP timeout/fail 근본 원인 해결**: status 폴링마다 messages 전체 반환 → compact 분리
- **메시지 비는 문제**: status(compact)와 result(full) 엔드포인트 분리로 해결
- **subprocess timeout**: 300s → 600s
- **SPLIT_PROMPT 압축**: 20줄 → 4줄 (payload 절감)
- **context 자동 압축**: debate 결과 2000자 cap

---

### 📊 성능 테스트 결과 (2026-03-21)
- pair2 (todo list API): fail 없이 완료
- debate → pair2 파이프라인 (JWT 인증): **79초 완료**, fail 0건
- compact status 패치 이전 대비 MCP fail 발생 없음

---

## v6.x 이전

- 초기 debate 엔진 구현 (Claude Generator + Gemini Critic 단일)
- pair 병렬 생성 모드 추가
- Flask 웹 UI 구현
- MCP 서버 연동 (7개 개별 도구)
