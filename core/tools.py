"""
core/tools.py — Improvement #10: Plugin/Tool 호출 지원

토론 중 AI가 사용할 수 있는 도구들.
결과는 프롬프트에 자동 주입.

Tools:
- web_search: DuckDuckGo 기반 검색
- code_exec:  샌드박스 Python 실행 (subprocess + timeout)
- file_read:  허용 경로 파일 읽기
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from .types import ToolResult


# ─── 허용 경로 (file_read용) ───
_ALLOWED_DIRS: List[Path] = [
    Path(__file__).parent.parent,  # debate-chain 프로젝트 루트
]


def _is_allowed_path(path: str) -> bool:
    p = Path(path).resolve()
    return any(p.is_relative_to(a.resolve()) for a in _ALLOWED_DIRS)


# ─── web_search ───

def web_search(query: str, max_results: int = 3) -> ToolResult:
    """DuckDuckGo 검색 (duckduckgo-search 패키지 필요)"""
    t0 = time.monotonic()
    try:
        from duckduckgo_search import DDGS
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return ToolResult("web_search", True, "검색 결과 없음",
                              elapsed_ms=_ms(t0))
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(
                f"[{i}] {r.get('title','')}\n"
                f"URL: {r.get('href','')}\n"
                f"{r.get('body','')[:400]}"
            )
        return ToolResult("web_search", True, "\n---\n".join(lines),
                          elapsed_ms=_ms(t0))
    except ImportError:
        return ToolResult("web_search", False,
                          error="pip install duckduckgo-search 필요",
                          elapsed_ms=_ms(t0))
    except Exception as e:
        return ToolResult("web_search", False, error=str(e)[:500],
                          elapsed_ms=_ms(t0))


# ─── code_exec ───

_EXEC_SANDBOX = Path(tempfile.gettempdir()) / "debate_chain_exec"
_EXEC_SANDBOX.mkdir(exist_ok=True)

_BLOCKED_IMPORTS = re.compile(
    r"\b(os\.system|subprocess|shutil\.rmtree|open\s*\([^)]*['\"]w['\"]|"
    r"exec\s*\(|eval\s*\(|__import__|importlib)\b"
)

def code_exec(code: str, timeout: int = 10) -> ToolResult:
    """
    샌드박스 Python 코드 실행.
    위험한 import/함수 패턴 차단 후 subprocess 실행.
    """
    t0 = time.monotonic()

    # 기본 위험 패턴 차단
    if _BLOCKED_IMPORTS.search(code):
        return ToolResult("code_exec", False,
                          error="보안 정책: 허용되지 않은 패턴 감지",
                          elapsed_ms=_ms(t0))

    tmp = _EXEC_SANDBOX / f"exec_{int(time.time()*1000)}.py"
    try:
        tmp.write_text(code, encoding="utf-8")
        result = subprocess.run(
            ["python", str(tmp)],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", shell=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        success = result.returncode == 0
        return ToolResult("code_exec", success,
                          output=output[:3000],
                          error=None if success else result.stderr[:500],
                          elapsed_ms=_ms(t0))
    except subprocess.TimeoutExpired:
        return ToolResult("code_exec", False, error=f"실행 시간 초과 ({timeout}s)",
                          elapsed_ms=_ms(t0))
    except Exception as e:
        return ToolResult("code_exec", False, error=str(e)[:500],
                          elapsed_ms=_ms(t0))
    finally:
        tmp.unlink(missing_ok=True)


# ─── file_read ───

def file_read(path: str, max_chars: int = 8000) -> ToolResult:
    """허용된 경로의 파일을 읽어 반환."""
    t0 = time.monotonic()
    if not _is_allowed_path(path):
        return ToolResult("file_read", False,
                          error=f"허용되지 않은 경로: {path}",
                          elapsed_ms=_ms(t0))
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...[TRUNCATED]"
        return ToolResult("file_read", True, output=content, elapsed_ms=_ms(t0))
    except FileNotFoundError:
        return ToolResult("file_read", False, error="파일 없음", elapsed_ms=_ms(t0))
    except Exception as e:
        return ToolResult("file_read", False, error=str(e)[:500], elapsed_ms=_ms(t0))


# ─── 프롬프트 자동 주입 ───

_TOOL_CALL_RE = re.compile(
    r"<tool:(\w+)>(.*?)</tool>",
    re.DOTALL | re.IGNORECASE,
)

def inject_tools(prompt: str) -> str:
    """
    프롬프트 내 <tool:xxx>...</tool> 태그를 실행하고 결과로 교체.

    AI 응답 예시:
        <tool:web_search>Python async best practices 2025</tool>
        <tool:code_exec>print(1+1)</tool>
        <tool:file_read>core/convergence.py</tool>
    """
    def _replace(m: re.Match) -> str:
        tool_name = m.group(1).lower()
        arg = m.group(2).strip()
        if tool_name == "web_search":
            result = web_search(arg)
        elif tool_name == "code_exec":
            result = code_exec(arg)
        elif tool_name == "file_read":
            result = file_read(arg)
        else:
            result = ToolResult(tool_name, False, error=f"알 수 없는 툴: {tool_name}")
        return result.to_prompt_block()

    return _TOOL_CALL_RE.sub(_replace, prompt)


def _ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 1)
