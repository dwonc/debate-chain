"""
core/provider.py — Improvement #3: Provider 추상화 + 하이브리드 Backend

CLIBackend: 현행 CLI 호출 방식 유지 (정체성 보존)
SDKBackend: 선택적 SDK 가속 (Anthropic SDK, OpenAI SDK)
FallbackProvider: CLIBackend 실패 시 SDKBackend 자동 전환

현재 orchestrator.py의 call_claude / call_codex / call_gemini를
이 추상화로 교체.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from .security import run_cli_stdin, run_cli_tempfile, load_secret


# ─── 기본 응답 ───

@dataclass
class ProviderResponse:
    text: str
    provider: str
    backend: str          # "cli" | "sdk"
    latency_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.text.startswith("[ERROR]")


# ─── 추상 인터페이스 ───

class ProviderBackend(ABC):
    name: str = "unknown"

    @abstractmethod
    def invoke(self, prompt: str, timeout: int = 180) -> ProviderResponse:
        ...

    def is_available(self) -> bool:
        return True


# ─── CLI Backends ───

class ClaudeCLIBackend(ProviderBackend):
    """claude -p <prompt> (stdin 방식)"""
    name = "claude-cli"

    def invoke(self, prompt: str, timeout: int = 180) -> ProviderResponse:
        t0 = time.monotonic()
        stdout, stderr, rc = run_cli_stdin(["claude", "-p", "/dev/stdin"], prompt, timeout)
        # claude CLI가 stdin을 지원 안 할 경우 -p 직접 전달 fallback
        if rc == 127 or not stdout:
            stdout, stderr, rc = run_cli_stdin(["claude", "-p", prompt], "", timeout)
        ms = int((time.monotonic() - t0) * 1000)
        err = stderr if rc != 0 else None
        return ProviderResponse(text=stdout or stderr, provider="claude", backend="cli", latency_ms=ms, error=err)


class CodexCLIBackend(ProviderBackend):
    """codex -q <prompt>"""
    name = "codex-cli"

    def invoke(self, prompt: str, timeout: int = 180) -> ProviderResponse:
        t0 = time.monotonic()
        stdout, stderr, rc = run_cli_stdin(["codex", "-q", "-"], prompt, timeout)
        if not stdout:
            stdout, stderr, rc = run_cli_stdin(["codex", "-q", prompt[:8000]], "", timeout)
        ms = int((time.monotonic() - t0) * 1000)
        err = stderr if rc != 0 else None
        return ProviderResponse(text=stdout or stderr, provider="codex", backend="cli", latency_ms=ms, error=err)


class GeminiAPIBackend(ProviderBackend):
    """Gemini API (google-generativeai SDK)"""
    name = "gemini-api"
    MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

    def invoke(self, prompt: str, timeout: int = 180) -> ProviderResponse:
        t0 = time.monotonic()
        try:
            import google.generativeai as genai
            api_key = load_secret("GEMINI_API_KEY")
            genai.configure(api_key=api_key)

            for model_name in self.MODELS:
                try:
                    model = genai.GenerativeModel(model_name)
                    response = model.generate_content(prompt)
                    ms = int((time.monotonic() - t0) * 1000)
                    return ProviderResponse(
                        text=response.text.strip(),
                        provider="gemini",
                        backend="sdk",
                        latency_ms=ms,
                    )
                except Exception as e:
                    if "quota" in str(e).lower() or "429" in str(e):
                        continue
                    raise
        except ImportError:
            ms = int((time.monotonic() - t0) * 1000)
            return ProviderResponse(
                text="", provider="gemini", backend="sdk", latency_ms=ms,
                error="google-generativeai not installed"
            )
        except Exception as e:
            ms = int((time.monotonic() - t0) * 1000)
            return ProviderResponse(
                text="", provider="gemini", backend="sdk", latency_ms=ms,
                error=str(e)[:500]
            )


# ─── SDK Backends (선택적) ───

class ClaudeSDKBackend(ProviderBackend):
    """Anthropic Python SDK (선택적 가속)"""
    name = "claude-sdk"

    def invoke(self, prompt: str, timeout: int = 180) -> ProviderResponse:
        t0 = time.monotonic()
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=load_secret("ANTHROPIC_API_KEY"))
            msg = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
            )
            ms = int((time.monotonic() - t0) * 1000)
            text = msg.content[0].text if msg.content else ""
            return ProviderResponse(
                text=text, provider="claude", backend="sdk", latency_ms=ms,
                tokens_in=msg.usage.input_tokens,
                tokens_out=msg.usage.output_tokens,
            )
        except ImportError:
            return ProviderResponse(
                text="", provider="claude", backend="sdk",
                latency_ms=int((time.monotonic() - t0) * 1000),
                error="anthropic SDK not installed. pip install anthropic"
            )
        except Exception as e:
            return ProviderResponse(
                text="", provider="claude", backend="sdk",
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(e)[:500]
            )


class OpenAISDKBackend(ProviderBackend):
    """OpenAI Python SDK (선택적 가속)"""
    name = "openai-sdk"

    def invoke(self, prompt: str, timeout: int = 180) -> ProviderResponse:
        t0 = time.monotonic()
        try:
            from openai import OpenAI
            client = OpenAI(api_key=load_secret("OPENAI_API_KEY"))
            resp = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=8192,
                timeout=timeout,
            )
            ms = int((time.monotonic() - t0) * 1000)
            text = resp.choices[0].message.content or ""
            return ProviderResponse(
                text=text, provider="openai", backend="sdk", latency_ms=ms,
                tokens_in=resp.usage.prompt_tokens if resp.usage else 0,
                tokens_out=resp.usage.completion_tokens if resp.usage else 0,
            )
        except ImportError:
            return ProviderResponse(
                text="", provider="openai", backend="sdk",
                latency_ms=int((time.monotonic() - t0) * 1000),
                error="openai SDK not installed. pip install openai"
            )
        except Exception as e:
            return ProviderResponse(
                text="", provider="openai", backend="sdk",
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=str(e)[:500]
            )


# ─── Fallback Provider (CLI → SDK 자동 전환) ───

@dataclass
class FallbackProvider:
    """
    primary_backend 실패 시 fallback_backend로 자동 전환.
    fallback 조건: error 포함 OR 빈 응답 OR 특정 에러 코드
    """
    name: str
    primary: ProviderBackend
    fallback: ProviderBackend
    fallback_on_empty: bool = True

    def invoke(self, prompt: str, timeout: int = 180) -> ProviderResponse:
        resp = self.primary.invoke(prompt, timeout)
        if resp.ok and (resp.text or not self.fallback_on_empty):
            return resp
        # fallback
        print(f"[FALLBACK] {self.primary.name} → {self.fallback.name}: {resp.error or 'empty'}")
        return self.fallback.invoke(prompt, timeout)


# ─── 팩토리 ───

def make_claude(prefer_sdk: bool = False) -> ProviderBackend:
    if prefer_sdk:
        return FallbackProvider(
            name="claude",
            primary=ClaudeSDKBackend(),
            fallback=ClaudeCLIBackend(),
        )
    return FallbackProvider(
        name="claude",
        primary=ClaudeCLIBackend(),
        fallback=ClaudeSDKBackend(),
    )


def make_codex(prefer_sdk: bool = False) -> ProviderBackend:
    if prefer_sdk:
        return FallbackProvider(
            name="codex",
            primary=OpenAISDKBackend(),
            fallback=CodexCLIBackend(),
        )
    return FallbackProvider(
        name="codex",
        primary=CodexCLIBackend(),
        fallback=OpenAISDKBackend(),
    )


def make_gemini() -> ProviderBackend:
    return GeminiAPIBackend()
