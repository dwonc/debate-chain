# Horcrux — 6-Model Adaptive AI Orchestration Engine

## Overview
6개 AI (Claude Opus 4.6, Codex 5.4, Gemini 2.5, Groq/Llama 3.3, DeepSeek Chat, OpenRouter GPT-OSS)를 수렴 기반 토론 루프로 오케스트레이션하는 시스템. 난이도를 자동 분류해서 최적 파이프라인으로 라우팅한다.

## Architecture
- `server.py`: Flask 메인 서버 (~2400줄). 통합 엔드포인트 /api/horcrux/run
- `planning_v2.py`: 3-AI 병렬 생성 -> Opus 합성 -> 5-Critic 병렬 평가 -> Revision Loop -> Polish
- `adaptive_orchestrator.py`: fast/standard/full 모드 분기 + interactive session 지원
- `mcp_server.js`: MCP 도구 6개 (run, check, classify, analytics, feedback, horcrux_test)
- `core/adaptive/`: 15개 모듈
  - classifier.py: intent detection + engine routing (heuristic-first)
  - interactive.py: pause/resume/rollback/feedback (cooperative threading.Event)
  - compact_memory.py: 3-layer memory + delta prompting
  - context_loader.py: .horcrux/ 프로젝트 context 로더
  - analytics.py: timeout auto-tuning + critic reliability

## Key Interfaces
- `planning_v2.inject_callers(call_claude, call_codex, call_gemini, call_aux_critic, aux_endpoints, ...)`
- `classify_task_complexity(task_description, ...) -> ClassificationResult` (mode + engine + intent)
- `InteractiveSession.check_pause_point()` -> cooperative pause at round boundaries
- `ProjectContext.load(project_dir)` -> .horcrux/context.md 로드

## Scoring
Final Score = Core min(Claude, Codex) * 0.8 + Aux avg(Gemini, Llama, DeepSeek, GPT-OSS) * 0.2
- Convergence threshold: 8.0/10
- Revision hard cap: 2 (standard), 5 (full)

## Conventions
- revision prompt에 tool-use 응답 생성 금지 -> 항상 content 직접 생성
- Gemini critic 빈 응답(confidence=0) -> score 계산에서 제외
- self-HTTP(requests.post localhost) -> 향후 ServiceDispatcher로 교체 예정
- .env 자동 로딩 (server.py 시작 시)

## Current State
- v8.0: Adaptive single entry point 완료
- Interactive Mode v3.0 구현 완료
- BUG-1~6 수정 완료
- .horcrux context file 기능 구현 중
- server.py 모듈 분리 (6-Phase) 예정
