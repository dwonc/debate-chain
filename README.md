# Horcrux

**6-Model Adaptive AI Orchestration Engine**

6개 AI 모델이 역할을 나눠 토론하고, 검증하고, 수렴합니다. 하나의 프롬프트만 넣으면 난이도를 자동 분류해서 최적 파이프라인으로 라우팅합니다.

## 정체성

Horcrux는 **코드 생성기가 아닙니다.** 코드 생성은 Claude Code 같은 단일 에이전트가 전체 컨텍스트를 유지하면서 짜는 게 더 빠르고 정확합니다.

Horcrux가 진짜 강한 영역은 **반복적 품질 개선이 필요한 작업**입니다:

- **서류/문서 작업** — 기획서, 보고서, 이력서 피드백. 초안 -> 5개 모델이 각자 관점에서 비판 -> 개선 -> 수렴할 때까지 반복.
- **코드 리팩토링** — 기존 코드를 3개 모델이 독립 분석 -> 통합 플랜 -> 5개 모델 크리틱 -> 리비전 반복. Deep Refactor 모드로 대형 프로젝트도 모듈별 자동 분할 분석.
- **의사결정/분석** — debate 모드로 찬반 검토, 여러 관점에서 검증.
- **브레인스토밍** — 3개 모델이 독립적으로 아이디어 생성 -> 종합 -> 검증.

핵심 원칙: **Generator != Critic != Synthesizer** — 자기확증 편향을 구조적으로 제거.

## 실험 기반 최적화 (v8.2)

24회 실행 실험(Opus vs Sonnet x standard/full x easy/medium/hard)으로 검증된 결과:

| 조합 | easy | medium | hard | **평균** |
|------|------|--------|------|---------|
| Opus+standard | 5.5 | 5.5 | 8.5 | 6.5 |
| **Opus+full** | **7.5** | 7.5 | **8.2** | **7.7** |
| Sonnet+standard | 4.0 | 5.5 | 5.0 | 4.8 |
| **Sonnet+full** | 7.5 | **8.5** | 7.5 | **7.5** |

**핵심 발견:**
- **모델보다 오케스트레이션 깊이가 중요** — Sonnet+full(7.5) > Opus+standard(6.5)
- **Full 모드에서 Opus/Sonnet 동점** — 평균 7.7 vs 7.5 (차이 0.2)
- **Sonnet은 비용 1/10에 full 모드로 Opus 동등 품질 달성**

이 데이터를 기반으로:
- **기본 모델: Sonnet** (비용 대비 품질 최적)
- **기본 모드: Full** (auto 라우팅 시 standard 대신 full)
- 간단한 수정만 Fast로 라우팅

### 6 Models, 5 Providers

| # | Model | Provider | Role |
|---|-------|----------|------|
| 1 | **Claude Sonnet 4.6** (기본) | Anthropic | Generator, Judge |
| 2 | **Codex 5.4** | OpenAI | Counter-Generator, Critic |
| 3 | **Gemini 2.5 Flash** | Google | Core Critic |
| 4 | **Llama 3.3 70B** | Meta (via Groq) | Aux Critic |
| 5 | **DeepSeek Chat** | DeepSeek | Aux Critic |
| 6 | **GPT-OSS 120B** | OpenRouter | Aux Critic |

```
Sonnet(Generator) -> Codex+Gemini+Llama+DeepSeek+GPT-OSS(5 Critics) -> Codex(Synthesizer)
                                    ^ score < 8.0 v
                              다음 라운드 (수렴까지 반복)
```

> 가중치는 critic 신뢰도 데이터 기반 자동 튜닝. 10회 완료마다 config.json 반영.

## Quick Start

```bash
git clone https://github.com/dwonc/horcrux.git
cd horcrux
pip install -r requirements.txt && npm install
cp .env.example .env   # API 키 입력
start.bat              # 또는 python server.py
```

## Modes

### Auto (기본) — 실험 데이터 기반 라우팅
```
Task -> Classifier -> intent + 난이도 감지 -> Full 또는 Fast 자동 선택
```

| 이렇게 말하면 | 이렇게 돌아감 |
|--------------|-------------|
| "typo 수정해줘" | fast -> adaptive_fast (30초) |
| "기능 추가해줘" | **full** -> adaptive_full (10분, 최고 품질) |
| "리팩토링 플랜" | **full** -> adaptive_full |
| "브레인스토밍해줘" | **full** -> planning_pipeline |
| "보안 감사" | **full** -> adaptive_full |
| "전체 코드 분석" | deep_refactor -> 멀티모델 분석 |

### Fast — 간단한 수정 전용
```
Sonnet(1-pass) -> Light Critic -> 결과        20~60초, 평균 5.0/10
```

### Full — 기본 모드 (실험 검증)
```
Sonnet+Codex(병렬) -> Synthesizer -> 5 Critics(병렬) -> Revision Loop -> 결과
평균 7.5/10, 3~10분
```

### Deep Refactor — 코드 리팩토링 분석 특화
```
Auto-Split -> 그룹별 x 3모델 분석 -> 종합 -> 5모델 크리틱 루프
```

### Parallel — 독립 작업 병렬 생성
```
Architect -> 2~3 AI 병렬 생성 (비판 없음, 순수 속도)
```

## 비용 효율성

| 조합 | 평균 점수 | 예상 비용/회 | 특징 |
|------|----------|-------------|------|
| **Sonnet+full (기본)** | **7.5** | **~$0.08** | **최고 가성비** |
| Opus+full | 7.7 | ~$0.80 | 최고 품질 (차이 미미) |
| Opus+standard | 6.5 | ~$0.40 | 비효율적 |
| Sonnet+standard | 4.8 | ~$0.04 | 품질 부족 |

> Opus로 전환: `claude_model=opus` 파라미터 추가

## Web UI

`http://localhost:5000`

## MCP (Claude Desktop)

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

## Architecture

```
                    /api/horcrux/run
                          |
                    [Classifier]
                   /      |      \
              Fast       Full     Special
               |          |         |
          adaptive    adaptive   planning / pair /
          _fast       _full      deep_refactor / self_improve
```

## Subscriptions

| Tier | 구독 | 월 비용 | 권장 |
|------|------|--------|------|
| **Recommended** | Claude Pro + ChatGPT Plus | **$40** | **Sonnet+full = Opus 동등 품질** |
| Maximum | Claude Max + ChatGPT Plus | $120 | Opus+full, 최고 품질 |
| Minimal | Claude Pro only | $20 | Sonnet + OpenAI API fallback |

Aux Critics(Groq, DeepSeek, OpenRouter)는 무료 tier로 운영됩니다.

## Project Structure

```
horcrux/
├── server.py                  # Flask 웹 서버 + 통합 API
├── adaptive_orchestrator.py   # CLI + 오케스트레이터
├── deep_refactor.py           # Deep Refactoring 파이프라인
├── .horcrux/optimization.md   # 실험 기반 최적 라우팅 정책
├── experiment_results/        # Opus vs Sonnet 실험 데이터
├── core/adaptive/             # classifier, analytics, ...
├── config.json                # scoring 가중치 (자동 튜닝)
└── logs/                      # 세션 로그
```

> 상세: [GUIDE.md](GUIDE.md) | 변경 이력: [CHANGELOG.md](CHANGELOG.md) | 실험: [experiment_report.md](experiment_results/experiment_report.md)

## License

MIT
