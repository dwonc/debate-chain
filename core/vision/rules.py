"""
VIS-002: design-rules.md 파서
마크다운 → 구조화된 dict 변환
"""
import re
from pathlib import Path
from typing import Optional


DEFAULT_RULES_PATH = Path(__file__).resolve().parent.parent.parent / ".horcrux" / "design-rules.md"


def parse_design_rules(path: Optional[str] = None) -> dict:
    """
    design-rules.md를 파싱하여 섹션별 dict 반환.

    Returns:
        {
            "Color Palette": {"primary": "#2563EB", ...},
            "Spacing": {"grid": "4px", ...},
            "Typography": {...},
            ...
            "_raw": "원본 마크다운 전체"
        }
    """
    p = Path(path) if path else DEFAULT_RULES_PATH
    if not p.exists():
        return {"_raw": "", "_error": f"File not found: {p}"}

    raw = p.read_text(encoding="utf-8")
    sections = {}
    current_section = None

    for line in raw.splitlines():
        line_stripped = line.strip()

        # ## Section Header
        m = re.match(r"^##\s+(.+)$", line_stripped)
        if m:
            current_section = m.group(1).strip()
            sections[current_section] = {}
            continue

        # Skip comments, empty lines, blockquotes
        if not line_stripped or line_stripped.startswith("#") or line_stripped.startswith(">") or line_stripped.startswith("<!--"):
            continue

        # - key: value
        m = re.match(r"^-\s+(\w[\w_]*)\s*:\s*(.+)$", line_stripped)
        if m and current_section:
            key = m.group(1).strip()
            val = m.group(2).strip()
            # 숫자 변환 시도
            if re.match(r"^\d+(\.\d+)?$", val):
                val = float(val) if "." in val else int(val)
            elif val.lower() == "true":
                val = True
            elif val.lower() == "false":
                val = False
            sections[current_section][key] = val

    sections["_raw"] = raw
    return sections


def rules_to_prompt(rules: dict) -> str:
    """파싱된 rules를 vision 프롬프트용 텍스트로 변환."""
    lines = ["## Design Rules to evaluate against:\n"]

    for section, values in rules.items():
        if section.startswith("_"):
            continue
        lines.append(f"### {section}")
        if isinstance(values, dict):
            for k, v in values.items():
                lines.append(f"- {k}: {v}")
        lines.append("")

    return "\n".join(lines)
