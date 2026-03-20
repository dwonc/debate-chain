# Debate Chain 🔗

**3-AI 멀티모델 오케스트레이션 시스템**

Claude Opus 4.6 + GPT-5.4 (Codex) + Gemini를 조합하여 코드 리뷰, 코드 생성, 기획 검증을 자동화합니다. 모든 AI는 **CLI 구독 기반**으로 호출되어 API 과금이 없습니다.

## 모드

### 🔍 Debate (3-AI 코드 리뷰)
```
Claude(생성) → Codex(Critic) + Gemini(Verifier) 병렬 → 수렴까지 반복
```
- 코드 품질 검증, 보안 리뷰, 아키텍처 평가
- 점수 8.0/10 이상이면 수렴 (converged)
- 최대 5라운드

### ⚡ Pair2 (2-AI 병렬 생성)
```
Claude(Architect) → Claude(Part1) + Codex(Part2) 병렬 생성
```
- Architect가 태스크를 자동으로 2파트로 분할
- 단일 모델 대비 **속도 ~50% 향상**

### 🚀 Pair3 (3-AI 병렬 생성)
```
Claude(Architect) → Claude(Part1) + Codex(Part2) + Gemini(Part3) 병렬 생성
```
- 3파트 분할, 풀스택+ML 프로젝트에 적합

## 사전 준비

| 도구 | 설치 | 구독 |
|------|------|------|
| Claude Code CLI | `npm install -g @anthropic-ai/claude-code` | Claude Max ($100/월) |
| Codex CLI | `npm install -g @openai/codex` | ChatGPT Plus ($20/월) |
| Gemini CLI | `npm install -g @anthropic-ai/gemini` 또는 별도 설치 | 무료 (할당량 제한) |
| Python 3.11+ | [python.org](https://python.org) | - |
| Node.js 18+ | [nodejs.org](https://nodejs.org) | - |

> 상세 설치 가이드는 [GUIDE.md](GUIDE.md) 참조

## 빠른 시작

```bash
# 1. 클론
git clone https://github.com/dwonc/debate-chain.git
cd debate-chain

# 2. 의존성 설치
pip install -r requirements.txt
npm install

# 3. 환경변수
cp .env.example .env
# .env 파일 편집하여 API 키 입력

# 4. 웹 서버 실행
python server.py
# → http://localhost:5000

# 5. (선택) MCP 서버 — Claude Desktop 연동
# claude_desktop_config.json에 debate-chain 추가 (GUIDE.md 참조)
```

## 사용법

### 웹 UI (http://localhost:5000)
- 브라우저에서 태스크 입력 → 실시간 라운드 진행 표시
- 스레드 히스토리, 점수 추이, 결과 복사

### MCP (Claude Desktop 연동)
Claude Desktop에서 자연어로 호출:
```
"이 코드 리뷰해줘"        → debate_start
"JWT 인증 만들어줘"       → pair2_start
"추천 시스템 3파트로"      → pair3_start
"연결 테스트"             → debate_test
```

### CLI
```bash
python orchestrator.py "Python으로 JWT 인증 구현해줘"
```

## 프로젝트 구조

```
debate-chain/
├── server.py          # Flask 웹 서버 (debate + pair 엔진)
├── mcp_server.js      # MCP 서버 (Claude Desktop 연동, Node.js)
├── orchestrator.py    # CLI 버전
├── config.json        # threshold, max_rounds 설정
├── start.bat          # 더블클릭 실행 (Windows)
├── package.json       # Node.js 의존성 (@modelcontextprotocol/sdk)
├── requirements.txt   # Python 의존성 (flask)
├── .env.example       # 환경변수 템플릿
├── docs/              # 디베이트 결과 문서
└── logs/              # 세션별 JSON 로그
```

## 아키텍처

### Debate 모드
```
┌─────────┐    ┌─────────┐    ┌──────────┐
│ Claude   │───→│ Codex   │───→│ Gemini   │
│Generator │    │ Critic  │    │ Verifier │
└────┬─────┘    └─────────┘    └──────────┘
     │              ↑ 병렬 실행 ↑
     │         avg(score) < 8.0
     └──────── Synthesizer ────────────────┘
```

### Pair 모드
```
┌──────────────┐
│ Claude       │ ── API Spec / 태스크 분할
│ Architect    │
└──────┬───────┘
       │
  ┌────┴────┐────────┐
  ↓         ↓        ↓
Claude   Codex    Gemini    ← 병렬 실행
Part 1   Part 2   Part 3
```

## 설정 (config.json)

```json
{
  "threshold": 8.0,      // 수렴 기준 점수
  "max_rounds": 5,       // 최대 라운드
  "models": {
    "generator": "claude-code-cli",
    "critic": "codex-cli",
    "verifier": "gemini-cli"
  }
}
```

## Gemini 모델 자동 폴백

할당량 소진 시 자동으로 다음 모델로 전환:
```
gemini-2.5-flash → gemini-2.0-flash → gemini-2.5-pro → gemini-1.5-flash
```

## 라이선스

MIT
