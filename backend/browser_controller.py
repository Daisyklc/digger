"""受控浏览器: 用 Playwright 驱动真实 Chromium/Edge 去打开/控制任意目标站。

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
import re

logger = logging.getLogger("WebTranslator.browser")

# Kindle Cloud Reader 壳层/加载中文案 (中英), 采到这些说明还没拿到正文
_CHROME_PATTERNS = [
    re.compile(r"学习阅读速度"),
    re.compile(r"Learning reading speed", re.I),
    re.compile(r"位于\s*\d+\s*/\s*\d+"),
    re.compile(r"Location\s*\d+\s*/\s*\d+", re.I),
    re.compile(r"\d+\s*/\s*\d+\s*[•●·]\s*\d+\s*%"),
]

_KINDLE_CONTENT_SELECTORS = (
    "#kr-renderer",
    "#kindleReader_content",
    ".kg-full-page",
    ".text-content",
    "#book-column-0",
    "[data-asin]",
)


def _headless_default() -> bool:
    return os.environ.get("WT_HEADLESS", "").lower() in ("1", "true", "yes")


def _browser_channel() -> str | None:
    """优先用本机已装浏览器, 避免依赖 playwright install 下载的 Chromium。

    WT_BROWSER_CHANNEL:
        msedge / chrome / chromium / ""(空=Playwright 自带 Chromium)
    未设置时 Windows 默认 msedge。
    """
    raw = os.environ.get("WT_BROWSER_CHANNEL")
    if raw is not None:
        val = raw.strip().lower()
        return val or None
    if os.name == "nt":
        return "msedge"
    return None


def _is_kindle_url(url: str) -> bool:
    u = (url or "").lower()
    return "read.amazon." in u or "kindle" in u


def _is_chrome_only(text: str) -> bool:
    """判断 DOM 文本是否只是 Kindle 壳层/进度条, 而非小说正文。"""
    if not text or not text.strip():
        return True
    compact = "".join(text.split())
    if len(compact) < 20:
        return True
    hits = sum(1 for p in _CHROME_PATTERNS if p.search(text))
    if hits >= 1 and len(compact) < 400:
        return True
    # 去掉壳层文案后几乎没剩下正文
    stripped = text
    for p in _CHROME_PATTERNS:
        stripped = p.sub("", stripped)
    stripped = re.sub(r"[\d\s/%•●·\-_|]+", "", stripped)
    return len(stripped) < 40


class BrowserController:
    """管理单个持久化浏览器上下文与当前页。线程内串行使用。"""

    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()
        from .config import settings

        self.user_data_dir = os.environ.get(
            "WT_BROWSER_PROFILE", settings.browser_profile
        )

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
                channel = _browser_channel()
                launch_kwargs: dict = {
                    "user_data_dir": self.user_data_dir,
                    "headless": hl,
                    "viewport": {"width": 1280, "height": 1600},
                }
                if channel:
                    launch_kwargs["channel"] = channel
                try:
                    self._ctx = await self._pw.chromium.launch_persistent_context(
                        **launch_kwargs
                    )
                except Exception as exc:
                    # 本机 channel 失败时回退到 Playwright Chromium, 并给出明确提示
                    if channel:
                        logger.warning(
                            "通过 channel=%s 启动失败 (%s), 尝试 Playwright Chromium",
                            channel,
                            exc,
                        )
                        launch_kwargs.pop("channel", None)
                        self._ctx = await self._pw.chromium.launch_persistent_context(
                            **launch_kwargs
                        )
                    else:
                        raise
                self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
                logger.info(
                    "受控浏览器已启动 (headless=%s, channel=%s)",
                    hl,
                    channel or "chromium",
                )
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

    async def _frames_for_content(self):
        """优先 Kindle 正文 iframe, 再退回主 frame。"""
        assert self._page is not None
        frames = list(self._page.frames)
        kindle = [
            f
            for f in frames
            if f != self._page.main_frame
            and (
                "kindle" in (f.name or "").lower()
                or "kindle" in (f.url or "").lower()
                or "iframeHtml" in (f.url or "")
                or "read.amazon" in (f.url or "").lower()
            )
        ]
        # 非空子 frame 也值得试 (Kindle 正文常在无名同源 iframe)
        others = [
            f
            for f in frames
            if f != self._page.main_frame and f not in kindle and (f.url or "") not in ("", "about:blank")
        ]
        ordered = kindle + others + [self._page.main_frame]
        # 去重保序
        seen = set()
        out = []
        for f in ordered:
            i = id(f)
            if i not in seen:
                seen.add(i)
                out.append(f)
        return out

    async def _read_dom_text(self, content_selector: str | None = None) -> str:
        """从主页面 + iframe 尽量抽取正文 DOM 文本。"""
        assert self._page is not None
        texts: list[str] = []

        if content_selector:
            try:
                el = await self._page.query_selector(content_selector)
                if el:
                    t = (await el.inner_text()).strip()
                    if t:
                        texts.append(t)
            except Exception as exc:  # noqa: BLE001
                logger.warning("content_selector 读取失败: %s", exc)

        for frame in await self._frames_for_content():
            try:
                for sel in _KINDLE_CONTENT_SELECTORS:
                    el = await frame.query_selector(sel)
                    if not el:
                        continue
                    t = (await el.inner_text()).strip()
                    if t and not _is_chrome_only(t):
                        texts.append(t)
                        break
                body = (await frame.inner_text("body")).strip()
                if body:
                    texts.append(body)
            except Exception as exc:  # noqa: BLE001
                logger.debug("frame 文本读取跳过: %s", exc)

        # 选「最不像壳层、最长」的一段
        candidates = [t for t in texts if t.strip()]
        if not candidates:
            return ""
        candidates.sort(key=lambda t: (0 if _is_chrome_only(t) else 1, len(t)), reverse=True)
        return candidates[0]

    async def _grab_canvas_shot(self) -> bytes | None:
        """尝试从 Kindle iframe 内 canvas 截取正文位图。"""
        assert self._page is not None
        for frame in await self._frames_for_content():
            try:
                canvases = await frame.query_selector_all("canvas")
                best = None
                best_area = 0
                for c in canvases:
                    box = await c.bounding_box()
                    if not box:
                        continue
                    area = box["width"] * box["height"]
                    if area > best_area:
                        best_area = area
                        best = c
                if best is not None and best_area >= 200 * 200:
                    return await best.screenshot()
            except Exception as exc:  # noqa: BLE001
                logger.debug("canvas 截图跳过: %s", exc)
        return None

    async def _progress_marker(self) -> str:
        """从壳层提取进度标记 (如 33/952), 用于判断是否翻页成功。"""
        assert self._page is not None
        try:
            text = await self._page.inner_text("body")
        except Exception:  # noqa: BLE001
            return ""
        m = re.search(r"(\d+)\s*/\s*(\d+)", text)
        return f"{m.group(1)}/{m.group(2)}" if m else ""

    @staticmethod
    def _progress_pos(marker: str | None) -> int | None:
        if not marker or "/" not in marker:
            return None
        try:
            return int(marker.split("/", 1)[0])
        except ValueError:
            return None

    async def wait_content_ready(
        self, timeout: float = 4.0, prev_progress: str | None = None
    ) -> None:
        """翻页后等待进度变化且连续两次相同, 避免截到连翻/动画中间帧。"""
        assert self._page is not None
        import time

        deadline = time.monotonic() + timeout
        await asyncio.sleep(0.3)
        last = None
        stable = 0
        changed = prev_progress is None
        while time.monotonic() < deadline:
            cur = await self._progress_marker()
            if prev_progress is not None and cur and cur != prev_progress:
                changed = True
            if cur and cur == last:
                stable += 1
                if changed and stable >= 2:
                    await asyncio.sleep(0.25)
                    return
            else:
                stable = 0
                last = cur
            await asyncio.sleep(0.15)
        logger.info("翻页后等待结束 (timeout=%.1fs), 继续采集", timeout)

    async def _focus_reader_without_turning(self) -> None:
        """让阅读器拿到键盘焦点, 但绝不点击正文区。

        Kindle Cloud Reader: 点击左/右半屏会翻页, 点中央也会触发布局/翻页。
        以前「先点中央再按键」等于一次翻多页, 造成采集跳页。
        """
        assert self._page is not None
        # 1) 尝试 focus 正文 iframe
        for frame in await self._frames_for_content():
            try:
                await frame.evaluate(
                    """() => {
                      const b = document.body;
                      if (!b) return false;
                      b.tabIndex = -1;
                      b.focus();
                      return true;
                    }"""
                )
                return
            except Exception:  # noqa: BLE001
                continue
        # 2) 点顶栏标题安全区 (不触发翻页)
        vp = self._page.viewport_size or {"width": 1280, "height": 1600}
        try:
            await self._page.mouse.click(max(80, vp["width"] // 2), 8)
        except Exception:  # noqa: BLE001
            pass

    async def capture(self, content_selector: str | None = None) -> dict:
        """采集当前页: 返回 {url, kind, text, fingerprint, screenshot_b64}。

        优先 DOM 文本(含 Kindle iframe); 壳层/过少则判定为图片型(canvas/DRM),
        优先 canvas 截图, 否则整页截图, 交由 OCR 处理。
        """
        async with self._lock:
            if self._page is None:
                raise RuntimeError("浏览器未启动")
            url = self._page.url
            # capture 内只做短 settle; 翻页后的进度等待由 next_page/live_session 负责
            await self.wait_content_ready(timeout=0.8)
            text = ""
            try:
                text = await self._read_dom_text(content_selector)
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取 DOM 文本失败: %s", exc)

            chrome_only = _is_chrome_only(text)
            kindle = _is_kindle_url(url)
            use_image = chrome_only or (kindle and chrome_only)

            shot = await self._grab_canvas_shot() if use_image else None
            if shot is None:
                shot = await self._page.screenshot(full_page=False)

            if use_image:
                kind = "image"
                # 图片指纹用截图, 避免壳层进度数字变化造成假翻页
                fp = hashlib.sha1(shot).hexdigest()[:16]
                text_out = ""
            else:
                kind = "text"
                compact = "".join(text.split())
                fp = hashlib.sha1(compact.encode("utf-8")).hexdigest()[:16]
                text_out = text

            logger.info(
                "采集完成 kind=%s chrome_only=%s text_len=%d shot=%d url=%s",
                kind,
                chrome_only,
                len(text_out),
                len(shot),
                url[:80],
            )
            return {
                "url": url,
                "kind": kind,
                "text": text_out,
                "fingerprint": fp,
                "screenshot_b64": base64.b64encode(shot).decode("ascii"),
                "screenshot_bytes": shot,
            }

    async def next_page(self, direction: str = "rtl") -> str | None:
        """翻页: 竖排日文(rtl)下一页=ArrowLeft; 横排(ltr)=ArrowRight。

        每次只触发一次翻页。禁止「点正文 + 按键 + 再点侧边」连翻。
        """
        async with self._lock:
            if self._page is None:
                raise RuntimeError("浏览器未启动")
            prev = await self._progress_marker()
            prev_n = self._progress_pos(prev)
            await self._focus_reader_without_turning()

            key = "ArrowRight" if direction == "ltr" else "ArrowLeft"
            await self._page.keyboard.press(key)

            import time

            deadline = time.monotonic() + 2.2
            cur = prev
            while time.monotonic() < deadline:
                await asyncio.sleep(0.15)
                cur = await self._progress_marker()
                if cur and prev and cur != prev:
                    break

            cur_n = self._progress_pos(cur)
            delta = (cur_n - prev_n) if (cur_n is not None and prev_n is not None) else None
            if delta:
                logger.info("翻页成功 %s → %s (Δ=%s, 仅按键一次)", prev, cur, delta)
                if abs(delta) > 40:
                    logger.warning("单次翻页 location 跳变偏大 Δ=%s", delta)
                return prev

            # 键盘无效时: 只尝试点圆形翻页按钮 (极靠屏幕边缘, 仍仅一次)
            vp = self._page.viewport_size or {"width": 1280, "height": 1600}
            hx = 4 if direction == "rtl" else max(4, vp["width"] - 4)
            hy = vp["height"] // 2
            logger.info("按键未改变进度 (%s), 边缘按钮补一次", prev)
            await self._page.mouse.click(hx, hy)
            await asyncio.sleep(0.45)
            logger.info("边缘点击后 %s → %s", prev, await self._progress_marker())
            return prev or None

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
