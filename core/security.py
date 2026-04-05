"""
core/security.py — Improvement #1: CLI 실행 경로 보안 강화

변경사항:
- shell=True 제거 → shell=False + argv 리스트
- temp file 제거 → stdin 직접 전달
- API key / 경로 / 토큰 redaction 파이프라인
- 프롬프트 길이 제한
- temp file 불가피할 시 보안 처리 (랜덤명, 0600, 즉시 삭제)
"""

import os
import re
import tempfile
import secrets
import subprocess
from pathlib import Path
from typing import Optional


# ─── 설정 ───
MAX_PROMPT_CHARS = 100_000
MAX_OUTPUT_BYTES = 512_000

# 민감 정보 패턴 (redaction용)
_REDACT_PATTERNS = [
    (re.compile(r"(sk-[A-Za-z0-9]{20,})", re.I),               "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"(AAAA[A-Za-z0-9_\-]{10,})", re.I),           "[REDACTED_FCM_KEY]"),
    (re.compile(r"(AIza[0-9A-Za-z\-_]{35})", re.I),            "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"(Bearer\s+[A-Za-z0-9\-._~+/]+=*)", re.I),    "[REDACTED_BEARER]"),
    (re.compile(r"password\s*=\s*['\"]?(\S+)['\"]?", re.I),    "password=[REDACTED]"),
    (re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z]{2,})", re.I), "[REDACTED_EMAIL]"),
]


def redact(text: str) -> str:
    """민감 정보를 마스킹한 문자열 반환"""
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_prompt(prompt: str) -> str:
    """프롬프트 길이 제한 + 기본 sanitize"""
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n...[TRUNCATED]"
    return prompt


_NPM_GLOBAL = os.path.join(os.environ.get("APPDATA", ""), "npm")


def _resolve_cli(cmd: list[str]) -> list[str]:
    """Windows에서 npm 글로벌 CLI(.cmd)를 자동 resolve."""
    if os.name != "nt" or not cmd:
        return cmd
    exe = cmd[0]
    # 이미 풀패스면 그대로
    if os.path.isfile(exe):
        return cmd
    # .cmd 확장자 추가해서 npm 글로벌에서 찾기
    cmd_path = os.path.join(_NPM_GLOBAL, f"{exe}.cmd")
    if os.path.isfile(cmd_path):
        return [cmd_path] + cmd[1:]
    return cmd


def run_cli_stdin(
    cmd: list[str],
    prompt: str,
    timeout: int = 300,
    env_extra: Optional[dict] = None,
) -> tuple[str, str, int]:
    """
    stdin으로 프롬프트를 전달하는 안전한 subprocess 실행.
    Returns: (stdout, stderr, returncode)
    """
    prompt = sanitize_prompt(prompt)
    cmd = _resolve_cli(cmd)

    env = {**os.environ}
    if env_extra:
        env.update(env_extra)

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            shell=False,       # ← 핵심: shell 주입 차단
            env=env,
        )
        stdout = result.stdout[:MAX_OUTPUT_BYTES] if result.stdout else ""
        stderr = redact(result.stderr[:2000]) if result.stderr else ""
        return stdout.strip(), stderr, result.returncode

    except subprocess.TimeoutExpired:
        return "", f"[TIMEOUT] {cmd[0]} exceeded {timeout}s", 1
    except FileNotFoundError:
        return "", f"[NOT_FOUND] '{cmd[0]}' CLI not found", 127
    except PermissionError:
        return "", f"[PERMISSION] Cannot execute '{cmd[0]}'", 126


def run_cli_tempfile(
    cmd_template: list[str],
    prompt: str,
    timeout: int = 300,
    placeholder: str = "{prompt_file}",
) -> tuple[str, str, int]:
    """
    stdin을 지원하지 않는 CLI를 위한 보안 temp file 실행.
    - 보안 임시 디렉터리 사용
    - 파일명 랜덤화
    - 실행 직후 즉시 삭제
    - Windows에서도 0600 권한 최대한 시도
    """
    prompt = sanitize_prompt(prompt)

    # 랜덤 파일명
    rand_suffix = secrets.token_hex(8)
    tmp_dir = Path(tempfile.gettempdir()) / "horcrux_secure"
    tmp_dir.mkdir(mode=0o700, exist_ok=True)
    tmp_path = tmp_dir / f"prompt_{rand_suffix}.txt"

    try:
        tmp_path.write_text(prompt, encoding="utf-8")
        try:
            tmp_path.chmod(0o600)
        except Exception:
            pass  # Windows에선 무시

        cmd = [placeholder if arg == placeholder else arg for arg in cmd_template]
        cmd = [str(tmp_path) if arg == placeholder else arg for arg in cmd_template]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        stdout = result.stdout[:MAX_OUTPUT_BYTES] if result.stdout else ""
        stderr = redact(result.stderr[:2000]) if result.stderr else ""
        return stdout.strip(), stderr, result.returncode

    except subprocess.TimeoutExpired:
        return "", f"[TIMEOUT] exceeded {timeout}s", 1
    except FileNotFoundError as e:
        return "", f"[NOT_FOUND] {e}", 127
    finally:
        # 반드시 즉시 삭제
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


# ─── R06: path traversal 차단 ───
_ALLOWED_ROOTS = [
    Path(p).resolve()
    for p in os.environ.get("HORCRUX_ALLOWED_ROOTS", "").split(os.pathsep)
    if p.strip()
]


def validate_project_dir(project_dir: str) -> Path:
    """project_dir이 허용된 루트 하위인지 검증. 실패 시 ValueError."""
    if not project_dir:
        raise ValueError("project_dir is empty")
    resolved = Path(project_dir).resolve()
    if not resolved.is_dir():
        raise ValueError(f"project_dir does not exist: {resolved}")
    if _ALLOWED_ROOTS:
        if not any(resolved.is_relative_to(root) for root in _ALLOWED_ROOTS):
            raise ValueError(
                f"project_dir '{resolved}' is outside allowed roots: "
                f"{[str(r) for r in _ALLOWED_ROOTS]}"
            )
    return resolved


def load_secret(key: str, default: str = "") -> str:
    """환경변수에서만 비밀값 로드 (config.json 금지)"""
    val = os.environ.get(key, default)
    if not val:
        print(f"[WARN] secret '{key}' not set in environment")
    return val
