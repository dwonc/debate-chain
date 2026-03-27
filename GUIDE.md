# Horcrux — Setup & Usage Guide

## 아키텍처

### v8 — Adaptive Single Entry Point

```
┌───────────────────────────────────────────────────────────┐
│                    Horcrux v8 Web UI                       │
│  ┌──────┬──────┬──────────┬──────┬──────────┐            │
│  │ Auto │ Fast │ Standard │ Full │ Parallel │            │
│  └──┬───┴──┬───┴────┬─────┴──┬───┴────┬─────┘            │
│     └──────┴────────┴────────┴────────┘                   │
│                     │                                     │
│              /api/horcrux/run                              │
│                     │                                     │
│              ┌──────▼──────┐                              │
│              │  Classifier  │ ← heuristic intent detect   │
│              └──────┬──────┘                              │
│     ┌───────┬───────┼───────┬──────────┬─────────┐       │
│     ▼       ▼       ▼       ▼          ▼         ▼       │
│  adaptive adaptive adaptive planning  pair     self     │
│  _fast    _standard _full   _pipeline _gen     _improve │
│                     debate                               │
│                     _loop                                │
└───────────────────────────────────────────────────────────┘
```

### 외부 모드 (사용자 선택)
| 모드 | 설명 | 색상 |
|------|------|------|
| **Auto** | task 분석 → 최적 경로 자동 선택 (기본) | 보라-파랑 |
| **Fast** | 간단한 수정, 저위험 | 초록 |
| **Standard** | 중간 복잡도, pair gen + critic | 노랑 |
| **Full** | 고난도, 풀체인 + aux critic | 빨강 |
| **Parallel** | 비판 없이 2~3 AI 병렬 생성 | 파랑 |

### 내부 엔진 (자동 결정)
| 엔진 | 용도 |
|------|------|
| `adaptive_fast` | 1-pass 생성 + light critic |
| `adaptive_standard` | pair gen + synth + core critic + revision(×2) |
| `adaptive_full` | full debate loop + aux critics + convergence |
| `debate_loop` | 멀티 라운드 debate (legacy) |
| `planning_pipeline` | 3 AI gen → synth → critic → polish (brainstorm/artifact) |
| `pair_generation` | 2~3 AI 병렬 코드 생성 |
| `self_improve` | 반복 개선 루프 |

### Intent 자동 감지
| Intent | 키워드 예시 | → 엔진 |
|--------|------------|--------|
| code_fix | fix, bug, typo, 수정, 오류 | adaptive_fast |
| feature_add | (기본) | adaptive_standard |
| refactor | refactor, architecture, 리팩토링 | adaptive_full |
| brainstorm | brainstorm, 아이디어, 전략, 기획 | planning_pipeline |
| artifact | ppt, pdf, 보고서, 포트폴리오 | planning_pipeline |
| parallel_gen | 병렬, parallel, 동시에, 나눠서 | pair_generation |
| self_improve | 개선, improve, 반복, 다듬어 | self_improve |

### 핵심 원칙
- Generator ≠ Critic ≠ Synthesizer — 자기확증 편향 구조적 제거
- 5개 모델, 4개 회사(Anthropic/OpenAI/Google/Meta), 3가지 아키텍처
- Aux 실패해도 Core 영향 없음 (graceful degradation)
- Phase 1.5 compact memory — 전체 재삽입 금지, delta만 전달


## 사전 요구사항

- Python 3.11+
- Node.js 18+ (MCP 서버용)
- Claude CLI: `npm install -g @anthropic-ai/claude-code`
- Codex CLI: `npm install -g @openai/codex` (선택)


## 구독별 설치

### 풀스펙 — Claude Max + GPT Pro ($400/월)
Opus 4.6 ↔ Codex 5.4 직접 debate. 최고 품질.
```bash
claude auth login    # Max 계정
codex auth login     # Pro 계정
```

### 균형 — Claude Pro + GPT Plus ($40/월)
Sonnet 4.6 ↔ GPT-4o debate.
```bash
claude auth login    # Pro 계정
codex auth login     # Plus 계정
```

### 최저 — Claude Pro only ($20/월)
Codex CLI 없어도 OpenAI API로 자동 전환.
```bash
claude auth login    # Pro 계정
pip install openai
# .env에 OPENAI_API_KEY 설정
```


## .env 설정

```env
# Codex fallback (CLI 미구독 시 자동 전환)
OPENAI_API_KEY=sk-proj-xxxx

# Aux Critics
GROQ_API_KEY=gsk_xxxx                    # 무료, 일 14,400회
DEEPSEEK_API_KEY=sk-xxxx                 # $0.27/1M input
OPENROUTER_API_KEY=sk-or-v1-xxxx         # $5 충전 → :free 모델 무제한

# 선택
GEMINI_API_KEY=AIzaSy-xxxx               # Gemini CLI용
```


## 실행

### Windows 원클릭
```
start.bat
```

### 수동
```bash
python server.py          # Web UI → http://localhost:5000
```

### Claude Desktop MCP 연동 (v8 — Node.js)
`claude_desktop_config.json`:
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

> **v7 이하에서 업그레이드 시**: `mcp_server.py` (Python) → `mcp_server.js` (Node.js)로 변경 필수.
> command: `"python"` → `"node"`, args: `"mcp_server.py"` → `"mcp_server.js"`

### MCP 도구 (v8 — 5개)
| 도구 | 설명 |
|------|------|
| `run` | 통합 실행. task만 넣으면 자동 라우팅. mode로 수동 override 가능. |
| `check` | job 상태 확인. 완료 시 결과 자동 포함. 모든 job_id prefix 대응. |
| `classify` | task 분류 미리보기. 어떤 모드/엔진이 선택될지 확인. |
| `analytics` | timeout 통계, 모드별 성능, critic 신뢰도 조회. |
| `horcrux_test` | AI 연결 테스트 (Claude, Codex). |

> **v7에서 제거된 도구**: `debate_start`, `debate_status`, `debate_result`, `pair2_start`, `pair3_start`, `pair_status`, `pair_result`, `self_improve` → 모두 `run` + `check`로 통합됨.


## 모드별 사용법

### Web UI (http://localhost:5000)
모드 버튼 5개: **Auto** / **Fast** / **Standard** / **Full** / **Parallel**

- **Auto**: 태스크 입력 → Run → 자동 분류 → 최적 엔진 실행
  - 옵션: Scope(auto/small/medium/large), Risk(auto/low/medium/high), Artifact(none/ppt/pdf/doc)
- **Fast/Standard**: 태스크 입력 → Run → 해당 모드 강제 실행
- **Full**: 태스크 입력 → Run → 풀체인 실행
  - 옵션: Artifact, Audience, Tone
- **Parallel**: 태스크 입력 → Run → 병렬 생성
  - 옵션: Parts(2/3), Output Dir

### Claude 모델 스위칭
- Web UI: input area 위 드롭다운 (Auto / Opus / Sonnet)
- MCP: "sonnet으로 돌려줘", "소넷", "use sonnet"
- API: `{"task": "...", "claude_model": "sonnet"}`

### CLI
```bash
# Auto (자동 분류 → 최적 엔진)
horcrux "fix typo in README"

# 모드 수동 지정
horcrux --mode fast "간단한 버그 수정"
horcrux --mode standard "새 API 엔드포인트 추가"
horcrux --mode full --risk high -f task.txt
horcrux --mode parallel --pair-mode pair3 "풀스택 앱"

# 분류만 미리보기 (실행 안 함)
horcrux classify "아키텍처 리팩토링"

# Flask 서버 경유 (웹 UI 스레드와 공유)
horcrux --server "브레인스토밍해줘"
horcrux --server classify "이 작업 뭘로 돌릴지"
```

> Windows: `horcrux.bat`, Linux/Mac: `./horcrux`
> 또는 직접: `python adaptive_orchestrator.py "task"`


## API 레퍼런스

### Horcrux v8 통합 (신규)
| 메서드 | 엔드포인트 | 설명 |
|--------|-----------|------|
| POST | /api/horcrux/run | **통합 실행** — classify → engine → 결과 |
| POST | /api/horcrux/classify | 분류 미리보기 (실행 안 함) |
| GET | /api/horcrux/status/{id} | 상태 조회 |
| GET | /api/horcrux/result/{id} | 결과 조회 |
| POST | /api/horcrux/stop/{id} | 중지 |

### Horcrux Run 파라미터
```json
POST /api/horcrux/run
{
  "task": "작업 내용 (필수)",
  "mode": "auto|fast|standard|full|parallel (기본: auto)",
  "scope": "small|medium|large (선택, auto-detect)",
  "risk": "low|medium|high (선택, auto-detect)",
  "artifact_type": "none|ppt|pdf|doc (선택)",
  "output_dir": "string (parallel only)",
  "project_dir": "string (프로젝트 context)",
  "audience": "string (문서/PPT 타겟 독자)",
  "tone": "professional|casual|technical",
  "iterations": "number (self_improve 횟수)",
  "pair_mode": "pair2|pair3 (parallel only)",
  "claude_model": "opus|sonnet"
}
```

### Horcrux Run 응답

**동기 응답** (adaptive_* 엔진):
```json
{
  "status": "completed|converged",
  "mode": "fast|standard|full",
  "internal_engine": "adaptive_fast|adaptive_standard|adaptive_full",
  "score": 8.5,
  "rounds": 2,
  "solution": "...",
  "routing": {"source": "heuristic", "confidence": 0.95, "intent": "code_fix"}
}
```

**비동기 응답** (planning/pair/debate/self_improve):
```json
{
  "status": "running",
  "job_id": "plan_20260327_...",
  "internal_engine": "planning_pipeline",
  "mode": "standard",
  "message": "Use check(job_id) to monitor",
  "routing": {"source": "heuristic", "confidence": 0.85, "intent": "brainstorm"}
}
```

### Core (기존 유지)
| 메서드 | 엔드포인트 | 설명 |
|--------|-----------|------|
| POST | /api/start | Debate 시작 |
| GET | /api/status/{id} | 상태 조회 |
| GET | /api/result/{id} | 전체 결과 |
| POST | /api/stop/{id} | 중지 |
| GET | /api/threads | 스레드 목록 |
| DELETE | /api/delete/{id} | 삭제 |
| GET | /api/test | 연결 테스트 |
| GET | /api/timing/{id} | 소요시간 |

### Pair (기존 유지)
| 메서드 | 엔드포인트 | 설명 |
|--------|-----------|------|
| POST | /api/pair | Pair2/Pair3 시작 |
| GET | /api/pair/status/{id} | Pair 상태 |
| GET | /api/pair/result/{id} | Pair 결과 |
| POST | /api/pair/stop/{id} | Pair 중지 |
| POST | /api/debate_pair | Debate→Pair 파이프라인 |

### Planning (기존 유지)
| 메서드 | 엔드포인트 | 설명 |
|--------|-----------|------|
| POST | /api/planning | Planning 시작 |
| GET | /api/planning/status/{id} | Planning 상태 |
| GET | /api/planning/result/{id} | Planning 결과 |
| POST | /api/planning/stop/{id} | Planning 중지 |

### Self-Improve (기존 유지)
| 메서드 | 엔드포인트 | 설명 |
|--------|-----------|------|
| POST | /api/self_improve | Self-Improve 시작 |
| GET | /api/self_improve/status/{id} | 상태 |
| GET | /api/self_improve/result/{id} | 결과 |

### Analytics (기존 유지)
| 메서드 | 엔드포인트 | 설명 |
|--------|-----------|------|
| GET | /api/analytics | 전체 대시보드 |
| GET | /api/analytics/timeouts | Timeout P50/P90/P99 + 추천 |
| POST | /api/analytics/timeouts/apply | Timeout 추천값 적용 |
| GET | /api/analytics/critics | Critic 신뢰도 |
| GET | /api/analytics/modes | 모드별 통계 |
| GET | /api/analytics/heuristic | Heuristic 미세 조정 추천 |

### 제거된 엔드포인트 (v8)
| 엔드포인트 | 대체 |
|-----------|------|
| /api/adaptive/run | → /api/horcrux/run |
| /api/adaptive/classify | → /api/horcrux/classify |
| /api/adaptive/status/{id} | → /api/horcrux/status/{id} |
| /api/adaptive/result/{id} | → /api/horcrux/result/{id} |
| /api/adaptive/stop/{id} | → /api/horcrux/stop/{id} |
| /api/adaptive/config | → /api/analytics |


## Codex Fallback Chain

```
1순위: Codex CLI (구독 tier 자동)
2순위: OpenAI SDK (GPT-4o-mini → GPT-4o)
3순위: Groq → Cerebras → OpenRouter (무료)
```


## Aux Critic 방어 구조

- 프롬프트: Core 60K, Aux 15K 압축
- 타임아웃: Core 300~900s, Aux 180s
- 429/402/404/네트워크 에러 → 스킵, Core 영향 없음
- Aux 전부 실패 → Core min으로 점수, debate 정상 진행
- 키 미설정 → 호출 자체 안 함
- Phase 2: conditional_aux — fast skip, high-risk activate


## 트러블슈팅

| 증상 | 해결 |
|------|------|
| Codex not found | .env에 OPENAI_API_KEY 설정 |
| Claude 타임아웃 | 60K 자동 truncation |
| Aux 응답 없음 | 자동 스킵, Core만 진행 |
| UI에 Aux 안 보임 | 서버 재시작 |
| MCP 도구 안 보임 | claude_desktop_config.json에서 `node` + `mcp_server.js`로 변경 확인 |
| MCP 모델 전환 안 됨 | Claude Desktop 재시작 |
| start.bat 경로 에러 | bat 파일이 horcrux 폴더 안에 있는지 확인 |
| Analytics 데이터 없음 | 세션 실행 후 로그 축적 필요 |
| 기존 adp_ 스레드 안 열림 | v8은 adp_ prefix 자동 호환, 서버 재시작 필요 |
