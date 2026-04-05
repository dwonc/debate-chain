"""
core/llm/callers.py — R12: AI Provider Caller Gateway (server.py에서 추출)

모든 LLM 호출은 이 모듈을 통해야 함.
- call_claude: Claude CLI (Opus/Sonnet)
- call_codex: Codex CLI → OpenAI SDK → Open Source 자동 전환
- call_gemini: Gemini API with model rotation
- call_gemini_fast: Flash-Lite 전용
- _call_aux_critic: Aux critics (Groq/DeepSeek/OpenRouter)
"""

import os
import platform
import shutil
import subprocess
import threading
import time

MAX_PROMPT_CHARS = 60000
MAX_PROMPT_RETRY = 30000

# R14: Gemini model rotation state
_gemini_current_model_idx = 0
_gemini_lock = threading.Lock()

GEMINI_MODELS = [
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
]

# R01: CLI 경로를 환경변수에서 읽음
_NPM = os.environ.get("CLI_BIN_DIR", "") or (
    os.path.join(os.environ.get("APPDATA", ""), "npm")
    if platform.system() == "Windows" else "/usr/local/bin"
)


# ── Claude 모델 스위칭 ──
CLAUDE_MODELS = {
    "opus": "claude-opus-4-6",       # Max 구독
    "sonnet": "claude-sonnet-4-6",   # Pro 구독
}


def _truncate_prompt(prompt: str, max_chars: int) -> str:
    """프롬프트 양끝 보존, 중간 잘라내기"""
    if len(prompt) <= max_chars:
        return prompt
    keep = max_chars // 2 - 80
    cut = len(prompt) - max_chars
    return (
        prompt[:keep]
        + f"\n\n...[TRUNCATED {cut} chars to fit context]...\n\n"
        + prompt[-keep:]
    )


def _win(name: str) -> str:
    return f"{_NPM}\\{name}.cmd"


def call_claude(prompt: str, timeout: int = 900, model: str = "claude-sonnet-4-6", _retry: int = 0) -> str:
    """Claude CLI - stdin 방식. model 파라미터로 Opus/Sonnet 전환. overloaded 시 1회 재시도.
    기본값: Sonnet (실험 결과: full 모드에서 Opus와 동등, 비용 1/10)"""
    import tempfile
    prompt = _truncate_prompt(prompt, MAX_PROMPT_CHARS)
    try:
        if platform.system() == "Windows":
            cmd = ["cmd", "/c", _win("claude"), "-p"]
        else:
            exe = shutil.which("claude") or "claude"
            cmd = [exe, "-p"]
        if model:
            cmd.extend(["--model", model])
        r = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            cwd=tempfile.gettempdir()
        )
        out = r.stdout.strip()
        if r.returncode != 0 and not out:
            return f"[ERROR] Claude (rc={r.returncode}): {r.stderr[:500]}"
        # overloaded_error 감지 → 30초 대기 후 1회 재시도
        if out and "overloaded" in out.lower() and _retry < 1:
            print(f"  [CLAUDE] Overloaded — retrying in 30s...")
            time.sleep(30)
            return call_claude(prompt, timeout, model, _retry=_retry + 1)
        return out if out else f"[ERROR] Claude empty: {r.stderr[:300]}"
    except subprocess.TimeoutExpired: return "[ERROR] Claude timeout"
    except FileNotFoundError: return "[ERROR] Claude CLI not found"
    except Exception as e: return f"[ERROR] Claude: {str(e)[:500]}"


def _call_openai_sdk(prompt: str, timeout: int = 180) -> str:
    """Codex fallback 1순위: OpenAI SDK (GPT-4o-mini → GPT-4o)"""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return ""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        for model in ["gpt-4o-mini", "gpt-4o"]:
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=16000, timeout=timeout,
                )
                text = resp.choices[0].message.content or ""
                if text.strip():
                    print(f"[FALLBACK] Codex CLI → OpenAI SDK/{model}")
                    return text.strip()
            except Exception as e:
                if any(kw in str(e).lower() for kw in ["rate", "quota", "billing"]):
                    continue
                raise
    except ImportError:
        try:
            import requests as _req
            for model in ["gpt-4o-mini", "gpt-4o"]:
                try:
                    r = _req.post("https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 16000},
                        timeout=timeout)
                    if r.status_code == 200:
                        text = r.json()["choices"][0]["message"]["content"].strip()
                        if text:
                            print(f"[FALLBACK] Codex CLI → OpenAI REST/{model}")
                            return text
                except Exception:
                    continue
        except ImportError:
            pass
    return ""


def _call_opensource_fallback(prompt: str, timeout: int = 120) -> str:
    """Codex fallback 2순위: 오픈소스 API (무료)"""
    try:
        import requests as _req
    except ImportError:
        return ""
    for name, base, env_key, model, extra_h in [
        ("Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile", {}),
        ("Cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "llama-3.3-70b", {}),
        ("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "meta-llama/llama-3.3-70b-instruct:free",
         {"HTTP-Referer": "https://github.com/horcrux", "X-Title": "Horcrux"}),
    ]:
        key = os.environ.get(env_key, "")
        if not key:
            continue
        try:
            h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            h.update(extra_h)
            r = _req.post(f"{base}/chat/completions", headers=h, json={
                "model": model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8192, "temperature": 0.7}, timeout=timeout)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            if text:
                print(f"[FALLBACK] → {name}/{model}")
                return text
        except Exception as e:
            print(f"[FALLBACK] {name} failed: {str(e)[:200]}")
    return ""


def _codex_fallback(prompt: str) -> str:
    """Codex CLI 실패 시 전체 fallback: OpenAI SDK → 오픈소스"""
    result = _call_openai_sdk(prompt)
    if result:
        return result
    result = _call_opensource_fallback(prompt)
    if result:
        return result
    return "[ERROR] Codex CLI failed. Set OPENAI_API_KEY (best) or GROQ_API_KEY (free) in .env"


def call_codex(prompt: str, timeout: int = 600) -> str:
    """Codex CLI → OpenAI SDK → Open Source API 자동 전환"""
    prompt = _truncate_prompt(prompt, MAX_PROMPT_CHARS)
    try:
        if platform.system() == "Windows":
            cmd = ["cmd", "/c", _win("codex"), "exec", "--skip-git-repo-check"]
        else:
            exe = shutil.which("codex") or "codex"
            cmd = [exe, "exec", "--skip-git-repo-check"]
        r = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        out = r.stdout.strip()
        if r.returncode == 0 and out and "[ERROR]" not in out:
            return out
        if r.returncode != 0 or not out:
            fb = _codex_fallback(prompt)
            if "[ERROR]" not in fb:
                return fb
        return out if out else f"[ERROR] Codex (rc={r.returncode}): {(r.stderr or '')[:500]}"
    except FileNotFoundError:
        return _codex_fallback(prompt)
    except subprocess.TimeoutExpired: return "[ERROR] Codex timeout"
    except Exception as e:
        fb = _codex_fallback(prompt)
        if "[ERROR]" not in fb:
            return fb
        return f"[ERROR] Codex: {str(e)[:500]}"


def _call_gemini_with_model(prompt: str, model: str, timeout: int = 300):
    """Gemini 호출. API 키 있으면 API(max_output_tokens 제어), 없으면 CLI fallback."""
    _all_gemini = set(GEMINI_MODELS) | set(GEMINI_FAST_MODELS)
    if model not in _all_gemini:
        return "[ERROR] Invalid Gemini model", "error"

    prompt = _truncate_prompt(prompt, MAX_PROMPT_CHARS)

    # ── 방법 1: Gemini API (GEMINI_API_KEY 있으면 우선 사용) ──
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        try:
            import requests as _req
            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            resp = _req.post(api_url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": 16384,
                    "temperature": 0.7,
                },
            }, timeout=timeout)
            if resp.status_code == 429:
                return None, "quota"
            if resp.status_code == 503:
                # 서비스 불가 (과부하/deprecation) → 다음 모델로 폴백
                return None, "quota"
            if resp.status_code == 404:
                # 모델 삭제됨 (deprecation) → 다음 모델로 폴백
                print(f"  [WARN] Gemini model {model} deprecated (404)")
                return None, "quota"
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts).strip()
                    if text:
                        return text, "ok"
                # empty response → 다음 모델로 폴백
                return None, "quota"
            else:
                err_text = resp.text[:300]
                if "quota" in err_text.lower() or "exhausted" in err_text.lower():
                    return None, "quota"
                return f"[ERROR] Gemini API {resp.status_code}: {err_text}", "error"
        except Exception as e:
            # API 실패 → CLI fallback
            pass

    # ── 방법 2: Gemini CLI (fallback) ──
    def _run(p: str, t: int):
        try:
            if platform.system() == "Windows":
                cmd = ["cmd", "/c", _win("gemini"), "--model", model]
            else:
                exe = shutil.which("gemini") or "gemini"
                cmd = [exe, "--model", model]
            r = subprocess.run(
                cmd, input=p, capture_output=True, text=True,
                timeout=t, encoding="utf-8", errors="replace", shell=False
            )
            out = r.stdout.strip()
            stderr = r.stderr or ""
            if "quota" in stderr.lower() or "exhausted" in stderr.lower():
                return None, "quota"
            if r.returncode != 0 and not out:
                return f"[ERROR] Gemini/{model}: {stderr[:300]}", "error"
            if not out:
                return None, "quota"  # empty → 다음 모델로 폴백
            return out, "ok"
        except subprocess.TimeoutExpired:
            return "[TIMEOUT]", "timeout"
        except FileNotFoundError:
            return "[ERROR] Gemini CLI not found", "error"
        except Exception as e:
            return f"[ERROR] Gemini: {str(e)[:300]}", "error"

    out, status = _run(prompt, timeout)
    if status == "timeout":
        short = _truncate_prompt(prompt, MAX_PROMPT_RETRY)
        out, status = _run(short, timeout)
        if status == "timeout":
            return "[ERROR] Gemini timeout", "error"
    return out, status


def call_gemini(prompt: str, timeout: int = 300) -> str:
    """R14: atomic model selection — lock covers read+update to prevent race."""
    global _gemini_current_model_idx
    for attempt in range(len(GEMINI_MODELS)):
        with _gemini_lock:
            idx = (_gemini_current_model_idx + attempt) % len(GEMINI_MODELS)
            model = GEMINI_MODELS[idx]
        result, status = _call_gemini_with_model(prompt, model, timeout)
        if status == "quota":
            with _gemini_lock:
                # R14: CAS — only advance if still pointing at the failed model
                if _gemini_current_model_idx == idx:
                    _gemini_current_model_idx = (idx + 1) % len(GEMINI_MODELS)
            continue
        if status == "ok":
            with _gemini_lock:
                _gemini_current_model_idx = idx
        return result
    return "[ERROR] Gemini: all models exhausted"


# --- Gemini Fast mode (3.1 Flash-Lite) ---
GEMINI_FAST_MODELS = [
    "gemini-3.1-flash-lite-preview",  # 3세대 fast 전용
    "gemini-2.0-flash-lite",          # 폴백
]

def call_gemini_fast(prompt: str, timeout: int = 60) -> str:
    """Fast 모드 전용 Gemini critic. Flash-Lite 우선, 실패 시 일반 모델로 폴백."""
    # 1차: Flash-Lite 계열
    for model in GEMINI_FAST_MODELS:
        result, status = _call_gemini_with_model(prompt, model, timeout)
        if status == "quota":
            continue
        if status == "ok":
            return result
    # 2차: 일반 Gemini 모델로 크로스 폴백
    for model in GEMINI_MODELS:
        result, status = _call_gemini_with_model(prompt, model, timeout)
        if status == "quota":
            continue
        return result
    return "[ERROR] Gemini Fast: all models exhausted"


# ═══════════════════════════════════════════
# Phase 2: DEBATE ENGINE v7
# Multi-Critic(Codex+Gemini 병렬) + Synthesizer=Codex + Regression detection + 다차원 수렴
# ═══════════════════════════════════════════
debates = {}


# ── Auxiliary Open Source API Critics ──

# Aux 3모델: Meta Llama(Dense) + DeepSeek V3(MoE 671B) + GPT-OSS(MoE 117B)
# 학습 데이터/아키텍처/편향이 전부 다르므로 비판 관점 극대화
AUX_CRITIC_ENDPOINTS = [
    ("Groq/Llama", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
     "llama-3.3-70b-versatile", {}),
    ("DS/DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY",
     "deepseek-chat", {}),
    ("OR/GPT-OSS", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
     "openai/gpt-oss-120b:free",
     {"HTTP-Referer": "https://github.com/horcrux", "X-Title": "Horcrux"}),
]

AUX_MAX_PROMPT_CHARS = 15000  # Aux는 핵심만 받음. Core는 60K 전체, Aux는 15K 압축

def _truncate_for_aux(prompt: str) -> str:
    """Aux critic용 프롬프트 압축. 앞뒤 보존, 중간 잘라내기."""
    if len(prompt) <= AUX_MAX_PROMPT_CHARS:
        return prompt
    keep = AUX_MAX_PROMPT_CHARS // 2 - 50
    cut = len(prompt) - AUX_MAX_PROMPT_CHARS
    return prompt[:keep] + f"\n\n...[AUX TRUNCATED {cut} chars]...\n\n" + prompt[-keep:]

def _call_aux_critic(name, base_url, env_key, model, extra_headers, prompt, timeout=180):
    """Aux critic API. 프롬프트 15K 압축 + timeout=180s (3분, 분석 시간 충분히 배정). 서비스 장애(429/네트워크)만 처리."""
    api_key = os.environ.get(env_key, "")
    if not api_key:
        return name, ""
    try:
        import requests as _req
        short_prompt = _truncate_for_aux(prompt)
        h = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        h.update(extra_headers)
        r = _req.post(f"{base_url}/chat/completions", headers=h, json={
            "model": model,
            "messages": [{"role": "user", "content": short_prompt}],
            "max_tokens": 8192, "temperature": 0.7,
        }, timeout=timeout)
        if r.status_code == 429:
            print(f"  [AUX] {name} rate limited (429), skipped")
            return name, ""
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        print(f"  [AUX] {name}/{model} responded ({len(text)} chars)")
        return name, text
    except Exception as e:
        print(f"  [AUX] {name} failed: {str(e)[:150]}")
        return name, ""

