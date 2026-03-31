"""
VIS-003: Vision UI Critic
스크린샷 + design-rules → Vision LLM 평가 → { score, issues[], suggestions[] }
"""
import json
import os
import re
import subprocess
import platform
import shutil
from pathlib import Path
from typing import Optional

from .rules import parse_design_rules, rules_to_prompt

# ── Gemini Vision (무료, 우선) ──────────────────────────────

GEMINI_VISION_MODELS = [
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

VISION_CRITIC_PROMPT = """You are a UI/UX design critic. Analyze the given screenshot and evaluate it against the design rules below.

{rules}

## Evaluation Criteria
1. **Alignment** — Are elements consistently aligned? Is the grid respected?
2. **Spacing** — Are margins/padding consistent and following the grid system?
3. **Color Harmony** — Does the color usage match the palette? Is contrast sufficient?
4. **Typography** — Is the heading hierarchy clear? Is text readable?
5. **Visual Balance** — Is the layout balanced? Does it feel cohesive?
6. **Responsiveness** — (if applicable) Does it adapt well to viewport: {viewport}?

## Output Format
Respond ONLY with valid JSON:
{{
  "score": <float 0.0-10.0>,
  "issues": [
    {{"severity": "critical|major|minor", "category": "<category>", "description": "<what's wrong>", "location": "<where on screen>"}}
  ],
  "suggestions": [
    "<actionable improvement suggestion>"
  ],
  "summary": "<1-2 sentence overall assessment>"
}}
"""


def _call_gemini_vision(
    image_base64: str,
    prompt: str,
    timeout: int = 60,
    mime_type: str = "image/png",
) -> Optional[str]:
    """Gemini Vision API 호출 — inlineData로 이미지 전달."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if not gemini_key:
        return None

    import requests as _req

    for model in GEMINI_VISION_MODELS:
        try:
            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            payload = {
                "contents": [{
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": image_base64,
                            }
                        },
                        {"text": prompt},
                    ]
                }],
                "generationConfig": {
                    "maxOutputTokens": 4096,
                    "temperature": 0.3,
                },
            }

            resp = _req.post(api_url, json=payload, timeout=timeout)

            if resp.status_code in (429, 503, 404):
                continue  # 다음 모델로 폴백

            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts).strip()
                    if text:
                        return text
                continue  # empty → 다음 모델

        except Exception:
            continue

    return None


def _call_claude_vision(
    image_base64: str,
    prompt: str,
    timeout: int = 120,
) -> Optional[str]:
    """Claude CLI를 통한 vision 폴백 — 임시 PNG 파일 경로 전달."""
    tmp_dir = Path(__file__).parent / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp_img = tmp_dir / "vision_capture.png"

    import base64
    tmp_img.write_bytes(base64.b64decode(image_base64))

    try:
        # claude CLI에 이미지 경로 + 프롬프트 전달
        if platform.system() == "Windows":
            cmd = ["cmd", "/c", "claude", "--print", "--image", str(tmp_img)]
        else:
            exe = shutil.which("claude") or "claude"
            cmd = [exe, "--print", "--image", str(tmp_img)]

        r = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    finally:
        tmp_img.unlink(missing_ok=True)

    return None


def _extract_json(text: str) -> Optional[dict]:
    """LLM 응답에서 JSON 추출."""
    # 코드블록 안의 JSON
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        text = m.group(1).strip()

    # 직접 JSON 파싱 시도
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # { ... } 블록 추출
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    return None


def vision_ui_critic(
    image_base64: str,
    viewport: str = "desktop",
    rules_path: Optional[str] = None,
    use_claude_fallback: bool = True,
    mime_type: str = "image/png",
) -> dict:
    """
    Vision UI Critic 메인 함수.

    Args:
        image_base64: PNG 스크린샷의 base64 인코딩
        viewport: 뷰포트 이름 (desktop/tablet/mobile)
        rules_path: design-rules.md 경로 (None이면 기본 경로)
        use_claude_fallback: Gemini 실패 시 Claude 폴백 사용 여부

    Returns:
        {
            "ok": bool,
            "score": float,
            "issues": [...],
            "suggestions": [...],
            "summary": str,
            "model_used": str,      # "gemini" | "claude"
            "error": str | None,
        }
    """
    result = {
        "ok": False,
        "score": 0.0,
        "issues": [],
        "suggestions": [],
        "summary": "",
        "model_used": "",
        "error": None,
    }

    # design rules 로딩
    rules = parse_design_rules(rules_path)
    rules_text = rules_to_prompt(rules)
    prompt = VISION_CRITIC_PROMPT.format(rules=rules_text, viewport=viewport)

    # 1차: Gemini Vision (무료)
    raw = _call_gemini_vision(image_base64, prompt, mime_type=mime_type)
    if raw:
        result["model_used"] = "gemini"
    elif use_claude_fallback:
        # 2차: Claude Vision (CLI)
        raw = _call_claude_vision(image_base64, prompt)
        if raw:
            result["model_used"] = "claude"

    if not raw:
        result["error"] = "All vision models failed"
        return result

    # JSON 파싱
    parsed = _extract_json(raw)
    if not parsed:
        result["error"] = f"Failed to parse JSON from response: {raw[:200]}"
        return result

    result["ok"] = True
    result["score"] = float(parsed.get("score", 0.0))
    result["issues"] = parsed.get("issues", [])
    result["suggestions"] = parsed.get("suggestions", [])
    result["summary"] = parsed.get("summary", "")

    return result


def run_vision_critic(
    url: str,
    viewport: str = "desktop",
    color_scheme: str = "light",
    rules_path: Optional[str] = None,
    save_screenshot: Optional[str] = None,
) -> dict:
    """
    캡처 → 평가 원스톱 함수.
    capture + critic을 하나로 연결.
    """
    from .capture import capture_screenshot

    cap = capture_screenshot(
        url=url,
        viewport=viewport,
        color_scheme=color_scheme,
        save_path=save_screenshot,
    )

    if not cap["ok"]:
        return {
            "ok": False,
            "capture_error": cap["error"],
            "score": 0.0,
            "issues": [],
            "suggestions": [],
            "summary": "",
            "model_used": "",
            "error": f"Capture failed: {cap['error']}",
        }

    critique = vision_ui_critic(
        image_base64=cap["png_base64"],
        viewport=viewport,
        rules_path=rules_path,
    )

    critique["url"] = url
    critique["viewport"] = viewport
    critique["color_scheme"] = color_scheme

    return critique


def analyze_image_file(
    file_path: str,
    viewport: str = "desktop",
    rules_path: Optional[str] = None,
) -> dict:
    """
    로컬 이미지 파일 → 바로 vision critic 분석.
    Playwright 캡처 없이 기존 이미지를 직접 전달.

    지원 포맷: PNG, JPG, JPEG, WEBP, GIF, BMP
    """
    import base64
    import mimetypes

    p = Path(file_path)
    if not p.exists():
        return {
            "ok": False, "score": 0.0, "issues": [], "suggestions": [],
            "summary": "", "model_used": "", "error": f"File not found: {file_path}",
        }

    # MIME type 추론
    mime, _ = mimetypes.guess_type(str(p))
    if not mime or not mime.startswith("image/"):
        return {
            "ok": False, "score": 0.0, "issues": [], "suggestions": [],
            "summary": "", "model_used": "", "error": f"Not an image file: {mime}",
        }

    raw_bytes = p.read_bytes()
    img_base64 = base64.b64encode(raw_bytes).decode("ascii")

    result = vision_ui_critic(
        image_base64=img_base64,
        viewport=viewport,
        rules_path=rules_path,
        mime_type=mime,
    )
    result["file_path"] = str(p)
    result["file_size"] = len(raw_bytes)
    result["mime_type"] = mime

    return result
