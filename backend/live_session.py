"""实时会话: 采集循环 + 手动翻页翻译。

OCR / 翻译 / 全自动 编排见 stages.StageManager。
本模块保留:
    - translate_current(): 手动模式——采集当前页 → 立即翻译 → 存盘
    - start_capture() / _capture_loop(): 只截图落盘 + 翻页
    - progress()/stop(): 兼容旧 UI, 转发到 stages
"""

from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from .browser_controller import controller
from .capture_queue import capture_queue
from .novel_session import NovelSession

logger = logging.getLogger("WebTranslator.live")


class LiveSession:
    def __init__(self) -> None:
        self.novel = NovelSession()
        self._capture_task: asyncio.Task | None = None
        self.reset_state()

    def reset_state(self) -> None:
        self.running = False
        self.capturing = False
        self.working = False  # 兼容旧字段; 真实 OCR/翻译状态在 stages
        self.book = "untitled"
        self.target_lang = "zh-CN"
        self.source_lang = "ja"
        self.series: str | None = None
        self.direction = "rtl"
        self.interval = 1.5
        self.polish = True
        self.index = 0
        self.pages_captured = 0
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

    async def _translate_capture(self, cap: dict, index: int) -> dict:
        """对一次采集结果做翻译 + 存盘。"""
        resp = await run_in_threadpool(
            self.novel.translate_page,
            book=self.book,
            index=index,
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
        out = await self._translate_capture(cap, self.index)
        self.index += 1
        self.message = f"已翻译第 {out['index']} 页"
        return out

    def _ensure_book_cfg(self, cfg: dict) -> None:
        self._apply_cfg(cfg)

    async def start_capture(self, cfg: dict) -> dict:
        """只启动采集: 截图落盘 + 翻页, 不跑 OCR/翻译。

        只追加策略:
            - 自动从该书已有最大 index+1 继续写, 绝不覆盖旧文件
            - 电子书翻到哪一页由人工保证; 不做去重/对齐
        """
        if self.capturing:
            return self.progress()
        if not controller.running:
            raise RuntimeError("浏览器未启动")
        self._ensure_book_cfg(cfg)
        max_pages = int(cfg.get("max_pages") or 2000)

        existing = await run_in_threadpool(capture_queue.existing_count, self.book)
        append_at = await run_in_threadpool(capture_queue.next_append_index, self.book)
        if existing == 0 and cfg.get("start_index") is not None:
            append_at = int(cfg.get("start_index") or 0)
        self.index = append_at
        self.pages_captured = existing

        await run_in_threadpool(capture_queue.write_manifest, self.book)

        self.running = True
        self.capturing = True
        if existing > 0:
            self.message = (
                f"续采追加中: 已有 {existing} 页, 从 index={append_at} 写入。"
                "请确认电子书已手动翻到衔接页。"
            )
        else:
            self.message = "采集中 (仅截图, 从 index=0 开始)…"
        self._capture_task = asyncio.create_task(self._capture_loop(max_pages))
        return self.progress()

    async def start_process(self, cfg: dict) -> dict:
        """兼容: 转发到 stages (OCR + 翻译)。"""
        from .stages import stages

        return await stages.start_process(cfg)

    async def start_auto(self, cfg: dict) -> dict:
        """兼容: 全自动运行。"""
        from .stages import stages

        return await stages.start_auto(cfg)

    async def _capture_loop(self, max_pages: int) -> None:
        """快路径: 只负责截图落盘 + 翻页, 绝不调用 OCR/翻译。"""
        from .stages import stages

        prev_fp = None
        dup_streak = 0
        try:
            while self.running and self.capturing and self.pages_captured < max_pages:
                pending = capture_queue.pending_count(self.book)
                ocr_busy = stages.is_running("ocr", self.book)
                self.message = f"采集第 {self.index} 页… (队列待处理 {pending})"
                if pending >= capture_queue.max_pending:
                    if ocr_busy or stages.is_running("translate", self.book):
                        ok = await run_in_threadpool(
                            capture_queue.wait_for_slot, self.book, 180.0
                        )
                        if not ok:
                            await asyncio.sleep(1.0)
                        continue
                    self.message = (
                        f"队列已满 ({pending}), 采集暂停。"
                        "请点「OCR」消化后再继续采集。"
                    )
                    break

                cap = await controller.capture()
                if cap["fingerprint"] == prev_fp:
                    dup_streak += 1
                    if dup_streak >= 2:
                        self.message = (
                            f"采集结束(疑似书末), 已截 {self.pages_captured} 页。"
                            "可点「OCR」开始识别。"
                        )
                        break
                else:
                    dup_streak = 0
                prev_fp = cap["fingerprint"]

                want = capture_queue.next_append_index(self.book)
                if want > self.index:
                    logger.warning(
                        "追加 index 对齐 %d → %d (避免覆盖)", self.index, want
                    )
                    self.index = want
                try:
                    await run_in_threadpool(
                        capture_queue.enqueue, self.book, self.index, cap
                    )
                except FileExistsError:
                    self.index = capture_queue.next_append_index(self.book)
                    await run_in_threadpool(
                        capture_queue.enqueue, self.book, self.index, cap
                    )
                self.pages_captured = capture_queue.existing_count(self.book)
                self.index += 1
                loc = await controller._progress_marker()
                self.message = (
                    f"已截图 {self.pages_captured} 页 ({loc or '?'}), "
                    f"下一 index={self.index}, 翻页中… "
                    f"(队列 {capture_queue.pending_count(self.book)})"
                )

                prev_prog = await controller.next_page(self.direction)
                await controller.wait_content_ready(
                    timeout=max(2.5, self.interval + 1.0),
                    prev_progress=prev_prog,
                )
                await asyncio.sleep(max(0.8, self.interval))
        except Exception as exc:  # noqa: BLE001
            logger.exception("采集循环异常")
            self.message = f"采集异常停止: {exc}"
        finally:
            self.capturing = False
            busy = stages.is_running("ocr", self.book) or stages.is_running(
                "translate", self.book
            )
            if not busy:
                self.running = False
            if "异常" not in self.message and "队列已满" not in self.message:
                self.message = (
                    f"采集完成 {self.pages_captured} 页 "
                    f"(待 OCR {capture_queue.stage_stats(self.book).get('ocr_pending', 0)})。"
                    "可点「OCR」开始识别。"
                )

    def stop(self) -> dict:
        from .stages import stages

        return stages.stop()

    def progress(self) -> dict:
        from .stages import stages

        return stages.progress(self.book if self.book else stages.focus_book)


live = LiveSession()
