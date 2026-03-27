# Horcrux

**6-Model Adaptive AI Orchestration Engine**

6개 AI 모델이 역할을 나눠 토론하고, 검증하고, 수렴합니다. 하나의 프롬프트만 넣으면 난이도를 자동 분류해서 최적 파이프라인으로 라우팅합니다.

### 6 Models, 5 Providers, 3 Architectures

| # | Model | Provider | Role |
|---|-------|----------|------|
| 1 | **Claude Opus 4.6** | Anthropic | Generator, Judge |
| 2 | **Codex 5.4** | OpenAI | Counter-Generator, Critic |
| 3 | **Gemini 2.5 Flash** | Google | Aux Critic |
| 4 | **Llama 3.3 70B** | Meta (via Groq) | Aux Critic |
| 5 | **DeepSeek Chat** | DeepSeek | Aux Critic |
| 6 | **GPT-OSS 120B** | OpenRouter | Aux Critic |

```
Claude(Generator) → Codex+Gemini+Llama+DeepSeek+GPT-OSS(5 Critics 병렬) → Codex(Synthesizer)
                                    ↑ score < 8.0 ↓
                              다음 라운드 (수렴까지 반복)
```

**점수 = Core min(Claude, Codex) × 0.8 + Aux avg(Gemini, Llama, DeepSeek, GPT-OSS) × 0.2**

Generator ≠ Critic ≠ Synthesizer — 자기확증 편향을 구조적으로 제거합니다.

## Quick Start

```bash
git clone https://github.com/dwonc/horcrux.git
cd horcrux
pip install -r requirements.txt && npm install
cp .env.example .env   # API 키 입력
python server.py        # http://localhost:5000
```

## CLI

```bash
horcrux "fix typo in README"                          # Auto — 자동 분류
horcrux --mode fast "간단한 버그 수정"                  # Fast — 즉시 처리
horcrux --mode full --risk high "보안 감사"             # Full — 6모델 풀체인
horcrux --mode parallel --pair-mode pair3 "풀스택 앱"   # Parallel — 3AI 병렬
horcrux classify "아키텍처 리팩토링"                     # 분류만 미리보기
horcrux --server "브레인스토밍해줘"                      # 서버 경유 (웹 UI 공유)
```

> Windows: `horcrux.bat`, Linux/Mac: `./horcrux` (또는 `python adaptive_orchestrator.py`)

## Modes

### Auto (기본)
```
Task → Classifier(heuristic) → intent + 난이도 자동 감지 → 최적 엔진 선택
```
모드를 몰라도 됩니다. 자연어로 말하면 알아서 라우팅.

| 이렇게 말하면 | 이렇게 돌아감 |
|--------------|-------------|
| "typo 수정해줘" | fast → adaptive_fast → 즉시 결과 |
| "브레인스토밍해줘" | standard → planning_pipeline → 4-phase |
| "아키텍처 리팩토링" | full → adaptive_full → debate loop |
| "PPT 만들어줘" | full → planning_pipeline → artifact 생성 |
| "3개 AI로 동시에" | parallel → pair_generation → 병렬 |
| "이 솔루션 3번 개선" | standard → self_improve → 반복 개선 |

### Fast
```
Claude(1-pass) → Light Critic → 결과        20~60초
```

### Standard
```
Claude+Codex(병렬 생성) → Synthesizer → Core Critic → Revision(max 2) → 결과        60~180초
```

### Full
```
Claude+Codex(병렬) → Synthesizer → 5 Critics(병렬) → Convergence → Revision Loop → 결과        3~10분
```

### Parallel
```
Architect(Claude) → 2~3 AI 병렬 생성 (비판 없음, 순수 속도)
```

## Web UI

`http://localhost:5000` — 모드 버튼 5개: **Auto** / **Fast** / **Standard** / **Full** / **Parallel**

## MCP (Claude Desktop)

`claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "horcrux": {
      "command": "node",
      "args": ["D:\\path\\to\\horcrux\\mcp_server.js"],
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

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Auto / Fast / Standard / Full / Parallel               │
│                     │                                   │
│              /api/horcrux/run                            │
│                     │                                   │
│              ┌──────▼──────┐                            │
│              │  Classifier  │ heuristic intent detect    │
│              └──────┬──────┘                            │
│     ┌───────┬───────┼───────┬──────────┬─────────┐     │
│     ▼       ▼       ▼       ▼          ▼         ▼     │
│  adaptive adaptive adaptive planning  pair     self   │
│  _fast    _standard _full   _pipeline _gen     _improve│
└─────────────────────────────────────────────────────────┘
```

## Subscriptions

| Tier | 구독 | 월 비용 | 품질 |
|------|------|--------|------|
| Full | Claude Max + ChatGPT Plus | $120 | Opus 4.6 + Codex 5.4 |
| Balanced | Claude Pro + ChatGPT Plus | $40 | Sonnet 4.6 + GPT-4o |
| Minimal | Claude Pro only | $20 | Sonnet + OpenAI API fallback |

Aux Critics(Groq, DeepSeek, OpenRouter)는 무료 tier로 운영됩니다.

## Project Structure

```
horcrux/
├── server.py                  # Flask 웹 서버 + 통합 API
├── adaptive_orchestrator.py   # CLI + 오케스트레이터
├── mcp_server.js              # MCP 서버 v8 (Claude Desktop)
├── horcrux / horcrux.bat      # CLI shortcut
├── planning_v2.py             # Planning 파이프라인
├── config.json                # 설정
├── core/
│   ├── adaptive/              # classifier, memory, analytics, fallback...
│   ├── provider.py            # 6-model provider 추상화
│   └── ...
├── docs/                      # 스펙 문서
├── logs/                      # 세션 로그
└── tests/                     # 테스트
```

> 상세: [GUIDE.md](GUIDE.md) | 변경 이력: [CHANGELOG.md](CHANGELOG.md)

## License

MIT
