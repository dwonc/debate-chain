# Horcrux

**Adaptive Multi-AI Orchestration Engine**

One prompt, best pipeline. Task를 자동 분류하고 최적 AI 파이프라인으로 라우팅합니다 — 간단한 1-pass 수정부터 멀티 라운드 debate loop, 병렬 코드 생성, PPT/PDF 아티팩트 생성까지.

## v8 — Single Entry Point

```
사용자: "이 코드 typo 수정해줘"     → auto → fast → 즉시 결과
사용자: "브레인스토밍해줘"           → auto → planning_pipeline → 4-phase
사용자: "아키텍처 리팩토링해줘"      → auto → adaptive_full → 2-round debate
사용자: "3개 AI로 동시에 만들어줘"   → auto → pair_generation → 병렬
사용자: "이 솔루션 3번 개선해줘"     → auto → self_improve → 반복 개선
```

**외부 모드 5개** (사용자가 볼 것): `Auto` / `Fast` / `Standard` / `Full` / `Parallel`
**내부 엔진 7개** (자동 선택): `adaptive_fast` / `adaptive_standard` / `adaptive_full` / `debate_loop` / `planning_pipeline` / `pair_generation` / `self_improve`

## 모드

### Auto (기본)
```
Task → Classifier(heuristic) → intent 감지 + 난이도 분류 → 최적 엔진 자동 선택
```
사용자는 모드를 몰라도 됨. 자연어로 말하면 Horcrux가 알아서 라우팅.

### Fast
```
Claude(1-pass) → Light Critic → Optional Revision → 결과
```
간단한 수정, 저위험. 20~60초.

### Standard
```
Pair Generation(Claude+Codex) → Synthesizer → Core Critic → Revision(max 2) → 결과
```
중간 복잡도. 60~180초.

### Full
```
Pair Generation → Synthesizer → Core Critic(2) → Aux Critic(3, 조건부) → Convergence → Revision Loop → 결과
```
고난도 아키텍처, 보안 감사, PPT/PDF 생성. 3~10분.

### Parallel
```
Architect(Claude) → 2~3 AI 병렬 생성 (비판 없음, 순수 속도)
```
파트별 분할 작업. Pair2(Claude+Codex) / Pair3(Claude+Codex+Gemini).

## 빠른 시작

```bash
# 1. 클론
git clone https://github.com/dwonc/horcrux.git
cd horcrux

# 2. 의존성
pip install -r requirements.txt
npm install

# 3. 환경변수
cp .env.example .env
# .env 파일 편집하여 API 키 입력

# 4. 실행
start.bat                    # Windows 원클릭
# 또는
python server.py             # Web UI → http://localhost:5000
```

## 사전 준비

| 도구 | 설치 | 구독 |
|------|------|------|
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` | Claude Max ($100/월) |
| Codex CLI | `npm install -g @openai/codex` | ChatGPT Plus ($20/월) |
| Gemini CLI | 별도 설치 | 무료 (할당량 제한) |
| Python 3.11+ | [python.org](https://python.org) | - |
| Node.js 18+ | [nodejs.org](https://nodejs.org) | - |

> 상세 설치 가이드는 [GUIDE.md](GUIDE.md) 참조

## 사용법

### Web UI (http://localhost:5000)
모드 버튼 5개로 전환: **Auto** / **Fast** / **Standard** / **Full** / **Parallel**

### MCP (Claude Desktop 연동)
```json
{
  "mcpServers": {
    "horcrux": {
      "command": "node",
      "args": ["D:\\Custom_AI-Agent_Project\\horcrux\\mcp_server.js"],
      "env": {
        "GROQ_API_KEY": "gsk_xxxx",
        "DEEPSEEK_API_KEY": "sk-xxxx",
        "OPENROUTER_API_KEY": "sk-or-v1-xxxx"
      }
    }
  }
}
```
MCP 도구 5개: `run` / `check` / `classify` / `analytics` / `horcrux_test`

### CLI
```bash
# Auto (자동 분류)
python adaptive_orchestrator.py "fix typo in README"

# 모드 수동 지정
python adaptive_orchestrator.py --mode fast "간단한 버그 수정"
python adaptive_orchestrator.py --mode full --risk high -f task.txt
python adaptive_orchestrator.py --mode parallel --pair-mode pair3 "풀스택 앱 만들어줘"

# 분류만 미리보기
python adaptive_orchestrator.py classify "아키텍처 리팩토링해줘"

# Flask 서버 경유 (웹 UI 스레드와 공유)
python adaptive_orchestrator.py --server "브레인스토밍해줘 AI 추천 시스템"
```

## 아키텍처

### Unified Entry Point (v8)
```
┌─────────────────────────────────────────────────────────┐
│                    Horcrux v8                            │
│  ┌──────┬──────┬──────────┬──────┬──────────┐          │
│  │ Auto │ Fast │ Standard │ Full │ Parallel │          │
│  └──┬───┴──┬───┴────┬─────┴──┬───┴────┬─────┘          │
│     │      │        │        │        │                 │
│     ▼      ▼        ▼        ▼        ▼                 │
│  Classifier → Internal Engine 자동 선택                  │
│  ┌─────────┬───────────┬──────────┬───────────┬───────┐│
│  │adaptive │adaptive   │adaptive  │planning   │pair   ││
│  │_fast    │_standard  │_full     │_pipeline  │_gen   ││
│  │         │           │debate    │self       │       ││
│  │         │           │_loop     │_improve   │       ││
│  └─────────┴───────────┴──────────┴───────────┴───────┘│
└─────────────────────────────────────────────────────────┘
```

### Debate Loop (Full mode)
```
┌──────────┐    ┌──────────────────────────┐    ┌────────────┐
│ Claude   │───→│ Codex+Gemini+Aux         │───→│ Codex      │
│ Generator│    │ 5 Critics (병렬)          │    │ Synthesizer│
└────┬─────┘    └──────────────────────────┘    └────────────┘
     │              ↑ score < 8.0                     │
     └────────────────────────────────────────────────┘
```

### Pair (Parallel mode)
```
┌──────────┐
│ Architect │── 태스크 분할
└─────┬────┘
  ┌───┴───┐───────┐
  ↓       ↓       ↓
Claude  Codex   Gemini   ← 병렬 생성
Part1   Part2   Part3
```

## 프로젝트 구조

```
horcrux/
├── server.py                  # Flask 웹 서버 (v8 통합 UI + API)
├── adaptive_orchestrator.py   # Adaptive 진입점 (CLI + 라이브러리)
├── mcp_server.js              # MCP 서버 v8.0 (Node.js, Claude Desktop)
├── mcp_server.py              # MCP 서버 (legacy Python)
├── orchestrator.py            # Legacy debate CLI
├── planning_v2.py             # Layer 3 Planning 파이프라인
├── config.json                # threshold, max_rounds, adaptive 설정
├── start.bat                  # Windows 원클릭 실행
├── core/
│   ├── adaptive/
│   │   ├── classifier.py      # v8 intent detection + engine routing
│   │   ├── config.py          # Timeout, routing, revision 설정
│   │   ├── stage_plan.py      # Mode별 stage 구성
│   │   ├── revision_gate.py   # Revision hard cap + 중단 판정
│   │   ├── timeout_budget.py  # Stage별 timeout + latency logging
│   │   ├── compact_memory.py  # 3-layer memory + checkpoint + delta
│   │   ├── writer_lock.py     # Single writer rule
│   │   ├── patch_format.py    # JSON patch 표준화
│   │   ├── conditional_aux.py # 조건부 aux 활성화
│   │   ├── artifact_spec.py   # Artifact spec 렌더링
│   │   ├── fallback_chain.py  # 고급 stage별 fallback
│   │   └── analytics.py       # Timeout tuning + critic reliability + dashboard
│   ├── provider.py            # CLI/API provider 추상화
│   ├── job_store.py           # SQLite job 저장
│   ├── security.py            # 보안
│   └── tools.py               # 도구
├── docs/                      # 기획 문서, 디베이트 결과
├── logs/                      # 세션별 JSON 로그 + latency jsonl
└── tests/                     # 테스트
```

## Analytics API

운영 데이터 기반 자동 최적화 대시보드:

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /api/analytics` | 전체 대시보드 (모든 메트릭 통합) |
| `GET /api/analytics/timeouts` | Stage별 P50/P90/P99 + timeout 추천 |
| `GET /api/analytics/critics` | Critic별 신뢰도 + 가중치 |
| `GET /api/analytics/modes` | 모드별 usage/score/latency |
| `GET /api/analytics/heuristic` | Heuristic 미세 조정 추천 |

## Gemini 모델 자동 폴백

할당량 소진 시 자동으로 다음 모델로 전환:
```
gemini-2.5-flash → gemini-2.0-flash → gemini-2.5-pro → gemini-1.5-flash
```

## 라이선스

MIT
