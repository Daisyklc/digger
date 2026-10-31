"""受控浏览器: 用 Playwright 驱动真实 Chromium 去打开/控制任意目标站。

这是「独立软件」的核心——它不是网页, 而是本地程序以操作系统级权限驱动一个真浏览器,
因此不受网页同源策略 / X-Frame-Options 限制, 能截图、翻页、读 DOM。

持久化 user-data-dir 保存登录态: 首次 headful 手动登录(含 2FA), 之后复用会话。
服务器/无显示环境可设 WT_HEADLESS=1 走无头(用于自动化测试)。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os

logger = logging.getLogger("WebTranslator.browser")


def _headless_default() -> bool:
    return os.environ.get("WT_HEADLESS", "").lower() in ("1", "true", "yes")


class BrowserController:
    """管理单个持久化浏览器上下文与当前页。线程内串行使用。"""

    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()
        self.user_data_dir = os.environ.get("WT_BROWSER_PROFILE", "./data/browser")

    @property
    def running(self) -> bool:
        return self._page is not None

    async def launch(self, url: str | None = None, headless: bool | None = None) -> dict:
        """启动(或复用)浏览器; 可选导航到 url。"""
        async with self._lock:
            if self._page is None:
                from playwright.async_api import async_playwright

                os.makedirs(self.user_data_dir, exist_ok=True)
                self._pw = await async_playwright().start()
                hl = _headless_default() if headless is None else headless
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    self.user_data_dir,
                    headless=hl,
                    viewport={"width": 1280, "height": 1600},
                )
                self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
                logger.info("受控浏览器已启动 (headless=%s)", hl)
            if url:
                await self._page.goto(url, wait_until="domcontentloaded")
            return await self._status_locked()

    async def goto(self, url: str) -> dict:
        async with self._lock:
            if self._page is None:
                raise RuntimeError("浏览器未启动")
            await self._page.goto(url, wait_until="domcontentloaded")
            return await self._status_locked()

    async def status(self) -> dict:
        async with self._lock:
            return await self._status_locked()

    async def _status_locked(self) -> dict:
        if self._page is None:
            return {"running": False, "url": None, "title": None}
        return {
            "running": True,
            "url": self._page.url,
            "title": await self._page.title(),
        }

    async def capture(self, content_selector: str | None = None) -> dict:
        """采集当前页: 返回 {url, kind, text, fingerprint, screenshot_b64}。

        优先 DOM 文本(可选中则质量最高); 文本过少则判定为图片型(canvas/DRM),
        返回整页截图位图交由 OCR 处理。
        """
        async with self._lock:
            if self._page is None:
                raise RuntimeError("浏览器未启动")
            url = self._page.url
            text = ""
            try:
                if content_selector:
                    el = await self._page.query_selector(content_selector)
                    text = (await el.inner_text()).strip() if el else ""
                if not text:
                    text = (await self._page.inner_text("body")).strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 DOM 文本失败: %s", exc)
            shot = await self._page.screenshot(full_page=False)
            compact = "".join(text.split())
            kind = "text" if len(compact) >= 20 else "image"
            fp = hashlib.sha1((compact or shot).__str__().encode()).hexdigest()[:16] \
                if compact else hashlib.sha1(shot).hexdigest()[:16]
            return {
                "url": url,
                "kind": kind,
                "text": text,
                "fingerprint": fp,
                "screenshot_b64": base64.b64encode(shot).decode("ascii"),
                "screenshot_bytes": shot,
            }

    async def next_page(self, direction: str = "rtl") -> None:
        """翻页: 竖排日文(rtl)下一页=ArrowLeft; 横排(ltr)=ArrowRight。"""
        async with self._lock:
            if self._page is None:
                raise RuntimeError("浏览器未启动")
            key = "ArrowRight" if direction == "ltr" else "ArrowLeft"
            await self._page.keyboard.press(key)

    async def close(self) -> None:
        async with self._lock:
            try:
                if self._ctx is not None:
                    await self._ctx.close()
                if self._pw is not None:
                    await self._pw.stop()
            finally:
                self._pw = self._ctx = self._page = None


controller = BrowserController()
