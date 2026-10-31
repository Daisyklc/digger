"""实时会话: 把受控浏览器与翻译内核串起来, 驱动两种模式。

- translate_current(): 手动模式——采集当前页 → 翻译 → 存盘 → 返回。
- start_auto(): 自动模式——后台循环: 采集 → 翻译存盘 → 翻页, 直到书末或手动停止。

进度优先: 翻译流水线持续推进并落盘, UI 通过 progress() 轮询(只读视图)。
翻译为同步调用(requests/openai), 放线程池执行, 不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from .browser_controller import controller
from .novel_session import NovelSession

logger = logging.getLogger("WebTranslator.live")


class LiveSession:
    def __init__(self) -> None:
        self.novel = NovelSession()
        self._task: asyncio.Task | None = None
        self.reset_state()

    def reset_state(self) -> None:
        self.running = False
        self.book = "untitled"
        self.target_lang = "zh-CN"
        self.source_lang = "ja"
        self.series: str | None = None
        self.direction = "rtl"
        self.interval = 1.5
        self.polish = True
        self.index = 0
        self.pages_done = 0
        self.message = "就绪"
        self.last_orig = ""
        self.last_translated = ""

    def _apply_cfg(self, cfg: dict) -> None:
        self.book = cfg.get("book") or "untitled"
        self.target_lang = cfg.get("target_lang") or "zh-CN"
        self.source_lang = cfg.get("source_lang") or "ja"
        self.series = cfg.get("series") or None
        self.direction = cfg.get("direction") or "rtl"
        self.interval = float(cfg.get("interval") or 1.5)
        self.polish = bool(cfg.get("polish", True))
        self.index = int(cfg.get("start_index") or 0)

    async def _translate_capture(self, cap: dict) -> dict:
        """对一次采集结果做翻译 + 存盘。"""
        resp = await run_in_threadpool(
            self.novel.translate_page,
            book=self.book,
            index=self.index,
            target_lang=self.target_lang,
            source_lang=self.source_lang,
            series=self.series,
            text=cap["text"] if cap["kind"] == "text" else None,
            image_bytes=cap.get("screenshot_bytes") if cap["kind"] == "image" else None,
            vertical=(self.direction == "rtl"),
            polish=self.polish,
        )
        self.last_orig = resp.orig
        self.last_translated = resp.polished or resp.translated
        self.pages_done = self.novel.info(self.book).pages_done
        return {
            "index": resp.index,
            "orig": resp.orig,
            "translated": self.last_translated,
            "kind": cap["kind"],
            "url": cap["url"],
        }

    async def translate_current(self, cfg: dict) -> dict:
        """手动模式: 翻译当前页一次(不翻页)。"""
        if not controller.running:
            raise RuntimeError("浏览器未启动")
        self._apply_cfg(cfg)
        cap = await controller.capture(cfg.get("content_selector"))
        out = await self._translate_capture(cap)
        self.index += 1
        self.message = f"已翻译第 {out['index']} 页"
        return out

    async def start_auto(self, cfg: dict) -> dict:
        if self.running:
            return self.progress()
        if not controller.running:
            raise RuntimeError("浏览器未启动")
        self._apply_cfg(cfg)
        max_pages = int(cfg.get("max_pages") or 2000)
        self.running = True
        self.message = "自动翻页中…"
        self._task = asyncio.create_task(self._auto_loop(max_pages))
        return self.progress()

    async def _auto_loop(self, max_pages: int) -> None:
        prev_fp = None
        try:
            while self.running and self.index < max_pages:
                cap = await controller.capture()
                if cap["kind"] == "image" and not cap["text"]:
                    # 图片型且 OCR 未接入, 记录但继续(避免空转死循环靠指纹判定)
                    logger.info("图片型页面, OCR 尚未接入, 第 %d 页", self.index)
                if cap["fingerprint"] == prev_fp:
                    self.message = f"已到书末, 共 {self.pages_done} 页"
                    break
                prev_fp = cap["fingerprint"]
                await self._translate_capture(cap)
                self.index += 1
                await controller.next_page(self.direction)
                await asyncio.sleep(self.interval)
        except Exception as exc:  # noqa: BLE001
            logger.exception("自动循环异常")
            self.message = f"异常停止: {exc}"
        finally:
            self.running = False
            if "书末" not in self.message and "异常" not in self.message:
                self.message = f"已停止, 共 {self.pages_done} 页"

    def stop(self) -> dict:
        self.running = False
        return self.progress()

    def progress(self) -> dict:
        return {
            "running": self.running,
            "book": self.book,
            "index": self.index,
            "pages_done": self.pages_done,
            "message": self.message,
            "last_orig": self.last_orig,
            "last_translated": self.last_translated,
        }


live = LiveSession()
