"""core/provider.py — v5: 2-Pair Core + Open Source Hybrid

Core Pair:
  - ClaudeCLI  (Opus 4.6) — Generator / Judge
  - CodexCLI   (Codex 5.4) — Counter-Generator / Critic

Auxiliary (무료 오픈소스 API):
  - OpenSourceAPI — Groq, Together, Fireworks, OpenRouter 등
  - 역할: 보조 Critic, Verifier, 사전 라우팅

변경점 (v4 → v5):
  - Gemini 의존성 완전 제거
  - OpenSourceAPIBackend 추가 (범용 OpenAI-compatible REST)
  - asyncio 기반 병렬 호출 지원
  - 단일 구독 (Claude Max) + 무료 API 구조
"""

from __future__ import annotations

import asyncio
import os
import time
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from concurrent.futures import ThreadPoolExecutor

from .security import run_cli_stdin, load_secret


# ─── 응답 ───

@dataclass
class ProviderResponse:
    text: str
    provider: str
    backend: str          # "cli" | "api"
    model: str = ""
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.text.startswith("[ERROR]")


# ─── 추상 인터페이스 ───

class ProviderBackend(ABC):
    name: str = "unknown"
    tier: str = "free"  # "paid" | "free"

    @abstractmethod
    def invoke(self, prompt: str, timeout: int = 300) -> ProviderResponse:
        ...

    def is_available(self) -> bool:
        return True


# ═══════════════════════════════════════════
#  CORE PAIR — CLI Backends (유료 구독)
# ═══════════════════════════════════════════

class ClaudeCLIBackend(ProviderBackend):
    """Claude Code CLI — 역할별 모델 분리 (Opus=생성/개선, Sonnet=비평/심판)"""
    name = "claude-cli"
    tier = "paid"

    def __init__(self, role: str = "generator"):
        """role: 'generator'|'synthesizer' → Opus, 'critic'|'judge' → Sonnet"""
        self.role = role
        if role in ("generator", "synthesizer"):
            self.model_id = "claude-opus-4-6"
            self.model_label = "opus-4.6"
        else:
            self.model_id = "claude-sonnet-4-6"
            self.model_label = "sonnet-4.6"

    def invoke(self, prompt: str, timeout: int = 300) -> ProviderResponse:
        t0 = time.monotonic()
        stdout, stderr, rc = run_cli_stdin(
            ["claude", "--model", self.model_id, "-p", "-"], prompt, timeout
        )
        if rc != 0 and not stdout.strip():
            if len(prompt) <= 8000:
                stdout, stderr, rc = run_cli_stdin(
                    ["claude", "--model", self.model_id, "-p", prompt], "", timeout
                )
            else:
                ms = int((time.monotonic() - t0) * 1000)
                return ProviderResponse(
                    text="", provider="claude", backend="cli",
                    model=self.model_label, latency_ms=ms,
                    error=f"CLI failed (prompt too long): {stderr[:300]}"
                )
        ms = int((time.monotonic() - t0) * 1000)
        err = stderr if rc != 0 else None
        return ProviderResponse(
            text=stdout.strip() or stderr,
            provider="claude", backend="cli",
            model=self.model_label, latency_ms=ms, error=err
        )


class CodexCLIBackend(ProviderBackend):
    """Codex CLI (5.4) — Counter-Generator + Critic"""
    name = "codex-cli"
    tier = "paid"

    def invoke(self, prompt: str, timeout: int = 300) -> ProviderResponse:
        t0 = time.monotonic()
        stdout, stderr, rc = run_cli_stdin(
            ["codex", "exec", "-"], prompt, timeout
        )
        if rc != 0 and not stdout.strip():
            if len(prompt) <= 8000:
                stdout, stderr, rc = run_cli_stdin(
                    ["codex", "exec", prompt], "", timeout
                )
        ms = int((time.monotonic() - t0) * 1000)
        err = stderr if rc != 0 else None
        return ProviderResponse(
            text=stdout.strip() or stderr,
            provider="codex", backend="cli",
            model="codex-5.4", latency_ms=ms, error=err
        )


class GeminiCLIBackend(ProviderBackend):
    """Gemini CLI (2.5 Pro) — Third Core: Generator + Critic"""
    name = "gemini-cli"
    tier = "paid"

    def __init__(self, model: str = "gemini-3-pro"):
        self.model_name = model

    def invoke(self, prompt: str, timeout: int = 300) -> ProviderResponse:
        t0 = time.monotonic()
        # Gemini CLI에 모델 명시 + non-interactive 모드
        stdout, stderr, rc = run_cli_stdin(
            ["gemini", "-m", self.model_name, "-p", "-"], prompt, timeout
        )
        ms = int((time.monotonic() - t0) * 1000)
        # "Loaded cached credentials" 등 stderr 메시지 필터링
        text = stdout.strip()
        if not text and stderr:
            # stderr에 실제 응답이 올 수 있음
            filtered = "\n".join(
                l for l in stderr.splitlines()
                if "cached credentials" not in l.lower() and l.strip()
            )
            if filtered and not filtered.startswith("[ERROR]"):
                text = filtered
        err = None if rc == 0 else stderr[:300]
        return ProviderResponse(
            text=text or stderr,
            provider="gemini", backend="cli",
            model="gemini-3.1-pro", latency_ms=ms, error=err
        )


# ═══════════════════════════════════════════
#  AUXILIARY — Open Source API (무료)
# ═══════════════════════════════════════════

# 사전 정의된 오픈소스 API 엔드포인트
OPENAI_COMPATIBLE_ENDPOINTS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "default_model": "llama-3.1-70b-versatile",
        "models": [
            "llama-3.1-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ],
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "env_key": "TOGETHER_API_KEY",
        "default_model": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
        "models": [
            "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
        ],
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "models": [
            "deepseek-chat",
            "deepseek-reasoner",
        ],
    },
    "fireworks": {
        "base_url": "https://api.fireworks.ai/inference/v1",
        "env_key": "FIREWORKS_API_KEY",
        "default_model": "accounts/fireworks/models/llama-v3p1-70b-instruct",
        "models": [
            "accounts/fireworks/models/llama-v3p1-70b-instruct",
            "accounts/fireworks/models/qwen2p5-coder-32b-instruct",
        ],
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "meta-llama/llama-3.1-70b-instruct:free",
        "models": [
            "meta-llama/llama-3.1-70b-instruct:free",
            "qwen/qwen-2.5-coder-32b-instruct:free",
            "deepseek/deepseek-chat:free",
        ],
    },
}


class OpenSourceAPIBackend(ProviderBackend):
    """
    OpenAI-compatible REST API 백엔드 (무료 오픈소스).
    Groq, Together, Fireworks, OpenRouter 등 모든 OpenAI-compatible 엔드포인트 지원.
    """
    name = "opensource-api"
    tier = "free"

    def __init__(self, endpoint: str = "groq", model: str = None):
        self.endpoint_name = endpoint
        ep_config = OPENAI_COMPATIBLE_ENDPOINTS.get(endpoint)
        if ep_config:
            self.base_url = ep_config["base_url"]
            self.env_key = ep_config["env_key"]
            self.model = model or ep_config["default_model"]
        else:
            # 커스텀 엔드포인트
            self.base_url = endpoint
            self.env_key = "CUSTOM_API_KEY"
            self.model = model or "unknown"
        self.name = f"{self.endpoint_name}-api"

    def is_available(self) -> bool:
        return bool(os.environ.get(self.env_key))

    # Aux 프롬프트 크기 제한 (Groq 413 / OpenRouter 429 방지)
    AUX_PROMPT_MAX_CHARS = 6000  # ~1.5k tokens — Groq 413 방지

    def _truncate_prompt(self, prompt: str) -> str:
        """Aux critic용 프롬프트 truncate — 무료 티어 payload/rate 제한 대응."""
        if len(prompt) <= self.AUX_PROMPT_MAX_CHARS:
            return prompt
        # 앞 70% + 뒤 30% (도입부+결론 우선)
        head = int(self.AUX_PROMPT_MAX_CHARS * 0.7)
        tail = self.AUX_PROMPT_MAX_CHARS - head
        return (
            prompt[:head]
            + "\n\n... [truncated] ...\n\n"
            + prompt[-tail:]
        )

    def invoke(self, prompt: str, timeout: int = 120) -> ProviderResponse:
        t0 = time.monotonic()
        api_key = os.environ.get(self.env_key, "")
        if not api_key:
            return ProviderResponse(
                text="", provider=self.endpoint_name, backend="api",
                model=self.model, latency_ms=0,
                error=f"{self.env_key} not set"
            )

        # Aux는 무료 티어 → 프롬프트 크기 제한
        prompt = self._truncate_prompt(prompt)

        try:
            import requests as _requests
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            # OpenRouter는 추가 헤더 필요
            if self.endpoint_name == "openrouter":
                headers["HTTP-Referer"] = "https://github.com/horcrux"
                headers["X-Title"] = "Horcrux"

            # Aux는 점수+짧은 피드백만 → max_tokens 절약
            max_tok = 1024 if len(prompt) < self.AUX_PROMPT_MAX_CHARS else 512
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tok,
                "temperature": 0.7,
            }

            # 429 retry with backoff (무료 티어 rate limit 대응)
            max_retries = 3 if self.endpoint_name in ("openrouter", "groq") else 1
            for attempt in range(max_retries):
                resp = _requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
                if resp.status_code == 429 and attempt < max_retries - 1:
                    wait = (attempt + 1) * 10  # 10s, 20s
                    import time as _time
                    _time.sleep(wait)
                    continue
                break
            resp.raise_for_status()
            data = resp.json()

            text = data["choices"][0]["message"]["content"].strip()
            usage = data.get("usage", {})
            ms = int((time.monotonic() - t0) * 1000)

            return ProviderResponse(
                text=text,
                provider=self.endpoint_name,
                backend="api",
                model=self.model,
                latency_ms=ms,
                tokens_in=usage.get("prompt_tokens", 0),
                tokens_out=usage.get("completion_tokens", 0),
                cost_usd=0.0,  # 무료 or 거의 무료
            )

        except ImportError:
            ms = int((time.monotonic() - t0) * 1000)
            return ProviderResponse(
                text="", provider=self.endpoint_name, backend="api",
                model=self.model, latency_ms=ms,
                error="requests not installed. pip install requests"
            )
        except Exception as e:
            ms = int((time.monotonic() - t0) * 1000)
            return ProviderResponse(
                text="", provider=self.endpoint_name, backend="api",
                model=self.model, latency_ms=ms,
                error=str(e)[:500]
            )


# ═══════════════════════════════════════════
#  병렬 호출 헬퍼
# ═══════════════════════════════════════════

_executor = ThreadPoolExecutor(max_workers=4)


def invoke_parallel(
    backends: List[ProviderBackend],
    prompt: str,
    timeout: int = 180,
) -> List[ProviderResponse]:
    """여러 백엔드를 동시 호출, 결과 리스트 반환."""
    futures = [
        _executor.submit(b.invoke, prompt, timeout)
        for b in backends
    ]
    results = []
    for f in futures:
        try:
            results.append(f.result(timeout=timeout + 10))
        except Exception as e:
            results.append(ProviderResponse(
                text="", provider="unknown", backend="parallel",
                error=f"Parallel invoke failed: {str(e)[:300]}"
            ))
    return results


async def invoke_parallel_async(
    backends: List[ProviderBackend],
    prompt: str,
    timeout: int = 180,
) -> List[ProviderResponse]:
    """asyncio 기반 병렬 호출 (이벤트 루프 내에서 사용)."""
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(_executor, b.invoke, prompt, timeout)
        for b in backends
    ]
    return await asyncio.gather(*tasks, return_exceptions=False)


# ═══════════════════════════════════════════
#  팩토리 — 구성 기반 인스턴스 생성
# ═══════════════════════════════════════════

def make_core_pair() -> Dict[str, ProviderBackend]:
    """핵심 2-pair: Claude + Codex"""
    return {
        "claude": ClaudeCLIBackend(role="generator"),
        "codex": CodexCLIBackend(),
    }


def make_core_trio() -> Dict[str, ProviderBackend]:
    """핵심 3-pair: Claude + Codex + Gemini (Core Trio)
    Claude는 역할별로 다른 인스턴스를 사용:
      - claude_gen: Opus (Generator/Synthesizer)
      - claude_crit: Sonnet (Critic/Judge)
    """
    return {
        "claude_gen": ClaudeCLIBackend(role="generator"),
        "claude_crit": ClaudeCLIBackend(role="critic"),
        "codex": CodexCLIBackend(),
        "gemini": GeminiCLIBackend(),
    }


def make_auxiliary(config: Dict = None) -> List[ProviderBackend]:
    """
    보조 오픈소스 백엔드 목록 생성.
    API 키가 있는 것만 활성화.
    """
    if config is None:
        config = {}

    aux_config = config.get("auxiliary", {})
    backends = []

    # config에 명시된 auxiliary 엔드포인트
    if aux_config:
        for ep_name, ep_settings in aux_config.items():
            model = ep_settings.get("model")
            backend = OpenSourceAPIBackend(endpoint=ep_name, model=model)
            if backend.is_available():
                backends.append(backend)
                print(f"  [AUX] {ep_name} ({backend.model}) ✅")
            else:
                print(f"  [AUX] {ep_name} — API key missing, skipped")
    else:
        # 자동 감지: 환경변수에 키가 있는 엔드포인트 자동 등록
        for ep_name in OPENAI_COMPATIBLE_ENDPOINTS:
            backend = OpenSourceAPIBackend(endpoint=ep_name)
            if backend.is_available():
                backends.append(backend)
                print(f"  [AUX:auto] {ep_name} ({backend.model}) ✅")

    return backends


def make_all(config: Dict = None) -> Dict[str, Any]:
    """
    전체 provider 세트 반환.
    {
        "core": {"claude": ..., "codex": ...},
        "auxiliary": [...],
    }
    """
    core = make_core_pair()
    aux = make_auxiliary(config)
    return {"core": core, "auxiliary": aux}
