"""
core/adaptive/context_loader.py — Project Context File (.horcrux) Loader

프로젝트 루트의 .horcrux/ 디렉토리에서 context.md, rules.md, config.json을 읽어
모든 AI 프롬프트에 통합 project context를 주입한다.

핵심:
  - .horcrux/context.md → 프로젝트 구조, 규칙, 인터페이스 (system prompt prefix)
  - .horcrux/rules.md → 코딩 규칙, 금지사항 (optional)
  - .horcrux/config.json → 프로젝트별 horcrux 설정 오버라이드 (optional)
  - 파일 없으면 빈 context 반환 (에러 아님)
  - KV 캐싱 효과: 동일 prefix → LLM이 자동 캐싱
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class ProjectContext:
    context_text: str = ""
    rules_text: str = ""
    config_overrides: Dict[str, Any] = field(default_factory=dict)
    project_dir: str = ""
    loaded: bool = False

    @classmethod
    def load(cls, project_dir: str) -> "ProjectContext":
        """프로젝트 루트에서 .horcrux/ 디렉토리를 찾아 context를 로드."""
        ctx = cls(project_dir=project_dir)
        horcrux_dir = Path(project_dir) / ".horcrux"

        if not horcrux_dir.is_dir():
            return ctx

        # context.md
        context_file = horcrux_dir / "context.md"
        if context_file.exists():
            try:
                ctx.context_text = context_file.read_text(encoding="utf-8").strip()
                ctx.loaded = True
            except Exception:
                pass

        # rules.md (optional)
        rules_file = horcrux_dir / "rules.md"
        if rules_file.exists():
            try:
                ctx.rules_text = rules_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        # config.json (optional)
        config_file = horcrux_dir / "config.json"
        if config_file.exists():
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    ctx.config_overrides = json.load(f)
            except Exception:
                pass

        return ctx

    def build_system_prefix(self, max_chars: int = 5000) -> str:
        """모든 AI 프롬프트 최상단에 삽입할 [PROJECT CONTEXT] 블록 생성."""
        if not self.context_text:
            return ""

        parts = []

        # context (필수)
        context = self.context_text[:max_chars]
        parts.append(f"[PROJECT CONTEXT]\n{context}\n[/PROJECT CONTEXT]")

        # rules (optional)
        if self.rules_text:
            remaining = max_chars - len(context)
            if remaining > 200:
                rules = self.rules_text[:remaining]
                parts.append(f"\n[PROJECT RULES]\n{rules}\n[/PROJECT RULES]")

        return "\n".join(parts)

    def get_config_override(self, key: str, default: Any = None) -> Any:
        """config.json에서 키 조회. 없으면 default 반환."""
        return self.config_overrides.get(key, default)

    def to_dict(self) -> dict:
        return {
            "project_dir": self.project_dir,
            "loaded": self.loaded,
            "context_length": len(self.context_text),
            "rules_length": len(self.rules_text),
            "config_keys": list(self.config_overrides.keys()),
        }
