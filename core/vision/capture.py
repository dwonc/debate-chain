"""
VIS-001: Playwright 스크린샷 캡처 모듈
URL → PNG 스크린샷 반환 (bytes)
"""
import asyncio
import base64
import time
from pathlib import Path
from typing import Optional, Literal

# Viewport 프리셋
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "tablet":  {"width": 768, "height": 1024},
    "mobile":  {"width": 375, "height": 812},
}

ViewportName = Literal["desktop", "tablet", "mobile"]
ColorScheme = Literal["light", "dark"]


async def _capture_async(
    url: str,
    viewport: ViewportName = "desktop",
    color_scheme: ColorScheme = "light",
    full_page: bool = True,
    timeout_ms: int = 30000,
    ignore_ssl: bool = False,
) -> bytes:
    """비동기 Playwright 캡처 — PNG bytes 반환."""
    from playwright.async_api import async_playwright

    vp = VIEWPORTS[viewport]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport=vp,
            color_scheme=color_scheme,
            ignore_https_errors=ignore_ssl,
        )
        page = await context.new_page()

        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        # 렌더링 안정화 대기
        await page.wait_for_timeout(500)

        screenshot = await page.screenshot(full_page=full_page, type="png")

        await browser.close()

    return screenshot


def _healthcheck(url: str, retries: int = 3, interval: float = 2.0) -> bool:
    """localhost dev server 헬스체크 (HTTP GET)."""
    import requests as _req

    for i in range(retries):
        try:
            resp = _req.get(url, timeout=5)
            if resp.status_code < 500:
                return True
        except Exception:
            pass
        if i < retries - 1:
            time.sleep(interval)
    return False


def capture_screenshot(
    url: str,
    viewport: ViewportName = "desktop",
    color_scheme: ColorScheme = "light",
    full_page: bool = True,
    timeout_ms: int = 30000,
    ignore_ssl: bool = False,
    healthcheck: bool = True,
    save_path: Optional[str] = None,
) -> dict:
    """
    메인 캡처 함수 (동기 래퍼).

    Returns:
        {
            "ok": bool,
            "png_bytes": bytes | None,
            "png_base64": str | None,    # vision API 전달용
            "viewport": str,
            "color_scheme": str,
            "url": str,
            "error": str | None,
        }
    """
    result = {
        "ok": False,
        "png_bytes": None,
        "png_base64": None,
        "viewport": viewport,
        "color_scheme": color_scheme,
        "url": url,
        "error": None,
    }

    # localhost URL이면 헬스체크
    is_local = any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0"))
    if healthcheck and is_local:
        if not _healthcheck(url):
            result["error"] = f"Dev server not responding: {url}"
            return result

    try:
        png = asyncio.run(_capture_async(
            url=url,
            viewport=viewport,
            color_scheme=color_scheme,
            full_page=full_page,
            timeout_ms=timeout_ms,
            ignore_ssl=ignore_ssl,
        ))

        result["ok"] = True
        result["png_bytes"] = png
        result["png_base64"] = base64.b64encode(png).decode("ascii")

        if save_path:
            Path(save_path).write_bytes(png)

    except Exception as e:
        result["error"] = str(e)

    return result


def capture_responsive(
    url: str,
    color_scheme: ColorScheme = "light",
    **kwargs,
) -> dict:
    """3종 뷰포트 (desktop/tablet/mobile) 일괄 캡처."""
    results = {}
    for vp_name in VIEWPORTS:
        results[vp_name] = capture_screenshot(
            url=url,
            viewport=vp_name,
            color_scheme=color_scheme,
            **kwargs,
        )
    return results
