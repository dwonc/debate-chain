"""core/llm — R12: Unified AI Provider Gateway."""

from .callers import (
    call_claude,
    call_codex,
    call_gemini,
    call_gemini_fast,
    _call_aux_critic,
    _truncate_prompt,
    _truncate_for_aux,
    CLAUDE_MODELS,
    GEMINI_MODELS,
    GEMINI_FAST_MODELS,
    AUX_CRITIC_ENDPOINTS,
    AUX_MAX_PROMPT_CHARS,
    MAX_PROMPT_CHARS,
    MAX_PROMPT_RETRY,
)

__all__ = [
    "call_claude", "call_codex", "call_gemini", "call_gemini_fast",
    "_call_aux_critic", "_truncate_prompt", "_truncate_for_aux",
    "CLAUDE_MODELS", "GEMINI_MODELS", "GEMINI_FAST_MODELS",
    "AUX_CRITIC_ENDPOINTS", "AUX_MAX_PROMPT_CHARS",
    "MAX_PROMPT_CHARS", "MAX_PROMPT_RETRY",
]
