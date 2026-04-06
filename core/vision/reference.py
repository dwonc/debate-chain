"""
VIS-006/007: Reference-based Vision Comparison Critic

.horcrux/references/ 폴더의 레퍼런스 디자인 이미지와
실제 캡처 스크린샷을 비교 평가.

사용법:
  result = compare_with_reference(
      capture_base64="...",           # 실제 캡처
      project_dir="D:/my-project",   # .horcrux/references/ 탐색
  )
"""

import base64
import json
import os
import re
from pathlib import Path
from typing import List, Optional

from .critic import _call_gemini_vision, _extract_json
from .rules import parse_design_rules, rules_to_prompt


# ── VIS-006: Reference Image Loader ──

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DEFAULT_REFERENCES_DIR = ".horcrux/references"


def load_references(project_dir: str, max_images: int = 5) -> List[dict]:
    """
    .horcrux/references/ 에서 레퍼런스 이미지를 로드.

    Returns:
        [
            {
                "name": "login-page.png",
                "base64": "iVBOR...",
                "mime_type": "image/png",
                "path": "D:/project/.horcrux/references/login-page.png",
            },
            ...
        ]
    """
    ref_dir = Path(project_dir) / DEFAULT_REFERENCES_DIR
    if not ref_dir.is_dir():
        return []

    refs = []
    for f in sorted(ref_dir.iterdir()):
        if f.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if len(refs) >= max_images:
            break
        try:
            raw = f.read_bytes()
            import mimetypes
            mime, _ = mimetypes.guess_type(str(f))
            refs.append({
                "name": f.name,
                "base64": base64.b64encode(raw).decode("ascii"),
                "mime_type": mime or "image/png",
                "path": str(f),
                "size_bytes": len(raw),
            })
        except Exception:
            continue

    return refs


# ── VIS-007: Comparison Critic ──

COMPARISON_PROMPT = """You are a UI/UX design comparison expert.

## Task
Compare the **Captured Screenshot** (the actual implementation) against the **Reference Design** (the intended design).

{rules}

## Reference Image
The first image is the REFERENCE (target design).

## Captured Screenshot
The second image is the ACTUAL IMPLEMENTATION to evaluate.

## Evaluation Criteria
1. **Layout Fidelity** — Does the layout match the reference? Position of elements, grid structure.
2. **Color Accuracy** — Are colors matching the reference palette? Any drift?
3. **Typography Match** — Font sizes, weights, hierarchy consistency.
4. **Spacing Consistency** — Margins, padding, gaps match the reference?
5. **Component Accuracy** — Buttons, cards, inputs, etc. look like the reference?
6. **Missing Elements** — Any elements in reference but missing in implementation?
7. **Extra Elements** — Any unexpected additions not in reference?

## Output Format
Respond ONLY with valid JSON:
{{
  "similarity_score": <float 0.0-10.0>,
  "layout_score": <float 0.0-10.0>,
  "color_score": <float 0.0-10.0>,
  "typography_score": <float 0.0-10.0>,
  "spacing_score": <float 0.0-10.0>,
  "differences": [
    {{"severity": "critical|major|minor", "category": "<category>", "description": "<what differs>", "location": "<where>"}}
  ],
  "missing_elements": ["<element missing from implementation>"],
  "extra_elements": ["<element not in reference>"],
  "suggestions": ["<how to fix>"],
  "summary": "<1-2 sentence comparison result>"
}}
"""


def compare_with_reference(
    capture_base64: str,
    project_dir: str,
    reference_name: Optional[str] = None,
    rules_path: Optional[str] = None,
    capture_mime: str = "image/png",
) -> dict:
    """
    VIS-007: 레퍼런스 이미지 vs 캡처 스크린샷 비교 평가.

    Args:
        capture_base64: 실제 캡처 스크린샷 base64
        project_dir: 프로젝트 루트 (.horcrux/references/ 탐색)
        reference_name: 특정 레퍼런스 파일명 (None이면 첫 번째 사용)
        rules_path: design-rules.md 경로
        capture_mime: 캡처 이미지 MIME type

    Returns:
        {
            "ok": bool,
            "similarity_score": float,
            "layout_score": float,
            ...
            "reference_used": str,
            "model_used": str,
            "error": str | None,
        }
    """
    result = {
        "ok": False,
        "similarity_score": 0.0,
        "layout_score": 0.0,
        "color_score": 0.0,
        "typography_score": 0.0,
        "spacing_score": 0.0,
        "differences": [],
        "missing_elements": [],
        "extra_elements": [],
        "suggestions": [],
        "summary": "",
        "reference_used": "",
        "model_used": "",
        "error": None,
    }

    # 레퍼런스 로드
    refs = load_references(project_dir)
    if not refs:
        result["error"] = f"No reference images found in {project_dir}/{DEFAULT_REFERENCES_DIR}"
        return result

    # 특정 레퍼런스 선택 또는 첫 번째
    ref = None
    if reference_name:
        ref = next((r for r in refs if r["name"] == reference_name), None)
        if not ref:
            result["error"] = f"Reference '{reference_name}' not found. Available: {[r['name'] for r in refs]}"
            return result
    else:
        ref = refs[0]

    result["reference_used"] = ref["name"]

    # design rules
    rules = parse_design_rules(rules_path)
    rules_text = rules_to_prompt(rules) if rules.get("_raw") else ""
    prompt = COMPARISON_PROMPT.format(rules=rules_text)

    # Gemini Vision에 두 이미지 전달
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        result["error"] = "GEMINI_API_KEY not set"
        return result

    raw = _call_gemini_vision_multi(
        images=[
            {"base64": ref["base64"], "mime_type": ref["mime_type"]},
            {"base64": capture_base64, "mime_type": capture_mime},
        ],
        prompt=prompt,
    )

    if not raw:
        result["error"] = "Gemini Vision comparison failed (all models exhausted)"
        return result

    result["model_used"] = "gemini"

    parsed = _extract_json(raw)
    if not parsed:
        result["error"] = f"Failed to parse comparison JSON: {raw[:200]}"
        return result

    result["ok"] = True
    for key in ("similarity_score", "layout_score", "color_score", "typography_score", "spacing_score"):
        result[key] = float(parsed.get(key, 0.0))
    result["differences"] = parsed.get("differences", [])
    result["missing_elements"] = parsed.get("missing_elements", [])
    result["extra_elements"] = parsed.get("extra_elements", [])
    result["suggestions"] = parsed.get("suggestions", [])
    result["summary"] = parsed.get("summary", "")

    return result


def _call_gemini_vision_multi(
    images: List[dict],
    prompt: str,
    timeout: int = 90,
) -> Optional[str]:
    """Gemini Vision API에 다중 이미지 전달."""
    from .critic import GEMINI_VISION_MODELS

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return None

    import requests as _req

    parts = []
    for img in images:
        parts.append({
            "inlineData": {
                "mimeType": img["mime_type"],
                "data": img["base64"],
            }
        })
    parts.append({"text": prompt})

    for model in GEMINI_VISION_MODELS:
        try:
            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": {
                    "maxOutputTokens": 4096,
                    "temperature": 0.3,
                },
            }
            resp = _req.post(api_url, json=payload, timeout=timeout)
            if resp.status_code in (429, 503, 404):
                continue
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    text_parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in text_parts).strip()
                    if text:
                        return text
                continue
        except Exception:
            continue

    return None


def run_comparison_critic(
    url: str,
    project_dir: str,
    viewport: str = "desktop",
    color_scheme: str = "light",
    reference_name: Optional[str] = None,
    rules_path: Optional[str] = None,
) -> dict:
    """
    캡처 → 레퍼런스 비교 원스톱 함수.
    capture + compare_with_reference를 하나로 연결.
    """
    from .capture import capture_screenshot

    cap = capture_screenshot(url=url, viewport=viewport, color_scheme=color_scheme)
    if not cap["ok"]:
        return {
            "ok": False,
            "error": f"Capture failed: {cap['error']}",
            "similarity_score": 0.0,
        }

    result = compare_with_reference(
        capture_base64=cap["png_base64"],
        project_dir=project_dir,
        reference_name=reference_name,
        rules_path=rules_path,
    )
    result["url"] = url
    result["viewport"] = viewport
    result["color_scheme"] = color_scheme

    return result
