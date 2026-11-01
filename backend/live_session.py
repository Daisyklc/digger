"""实时会话: 采集 / OCR / 翻译 三阶段解耦。

- translate_current(): 手动模式——采集当前页 → 立即翻译 → 存盘。
- start_auto(): 自动模式双协程
    * 采集循环 (快): 截图落盘 → 翻页
    * 消费循环 (慢): 读本地队列 → OCR → 翻译落盘 → 按配额删图

进度优先: UI 通过 progress() 轮询采集进度、队列积压、翻译进度。
"""

from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from .browser_controller import controller
from .capture_queue import capture_queue
from .novel_session import NovelSession

logger = logging.getLogger("WebTranslator.live")


def _write_ocr_sidecar(book: str, index: int, text: str) -> None:
    """把每页 OCR 日文落到 captures/{book}/{index}.ocr.txt, 便于抽查。"""
    from pathlib import Path

    from .capture_queue import capture_queue

    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in book)
    path = Path(capture_queue.root) / safe / f"{index:06d}.ocr.txt"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or "", encoding="utf-8")
    except OSError as exc:
        logger.warning("写 OCR sidecar 失败: %s", exc)


class LiveSession:
    def __init__(self) -> None:
        self.novel = NovelSession()
        self._capture_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._worker_stop = False
        self.reset_state()

    def reset_state(self) -> None:
        self.running = False
        self.capturing = False
        self.working = False
        self._worker_stop = False
        self.book = "untitled"
        self.target_lang = "zh-CN"
        self.source_lang = "ja"
        self.series: str | None = None
        self.direction = "rtl"
        self.interval = 1.5
        self.polish = True
        self.index = 0  # 下一张待采集页号
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

        # 磁盘断点优先于内存; start_index 仅在目录为空时生效
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
        """只启动后台处理: 消费本地截图队列做 OCR + 翻译。"""
        if self.working:
            return self.progress()
        self._ensure_book_cfg(cfg)
        # 允许对上次 OCR 失败但仍有截图的任务重试
        revived = await run_in_threadpool(capture_queue.requeue_failed, self.book)
        self.running = True
        self.working = True
        pending = capture_queue.pending_count(self.book)
        self.message = (
            f"处理队列中… (待处理 {pending}"
            + (f", 重试失败 {revived}" if revived else "")
            + ")"
        )
        self._worker_task = asyncio.create_task(self._worker_loop())
        return self.progress()

    async def start_auto(self, cfg: dict) -> dict:
        """采集 + 处理一起开 (采集仍不阻塞在 OCR 上)。"""
        await self.start_capture(cfg)
        await self.start_process(cfg)
        self.message = "采集+处理并行中 (截图先行, 翻译随后)…"
        return self.progress()

    async def _capture_loop(self, max_pages: int) -> None:
        """快路径: 只负责截图落盘 + 翻页, 绝不调用 OCR/翻译。"""
        prev_fp = None
        dup_streak = 0
        try:
            while self.running and self.capturing and self.pages_captured < max_pages:
                pending = capture_queue.pending_count(self.book)
                self.message = f"采集第 {self.index} 页… (队列待处理 {pending})"
                # 队列满: 若后台在处理则反压等待; 否则停采集让用户先处理
                if pending >= capture_queue.max_pending:
                    if self.working:
                        ok = await run_in_threadpool(
                            capture_queue.wait_for_slot, self.book, 180.0
                        )
                        if not ok:
                            await asyncio.sleep(1.0)
                        continue
                    self.message = (
                        f"队列已满 ({pending}), 采集暂停。"
                        "请点「处理队列」消化后再继续采集。"
                    )
                    break

                cap = await controller.capture()
                if cap["fingerprint"] == prev_fp:
                    dup_streak += 1
                    if dup_streak >= 2:
                        self.message = (
                            f"采集结束(疑似书末), 已截 {self.pages_captured} 页。"
                            "可点「处理队列」开始 OCR/翻译。"
                        )
                        break
                else:
                    dup_streak = 0
                prev_fp = cap["fingerprint"]

                # 防御并发/重启导致 index 撞车: 入队前再对齐一次追加点
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
                    # 极端竞态: 跳到下一个空位再写一次
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

                # 每次只翻一屏; 翻完等进度稳定再采下一张
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
            if not self.working:
                self.running = False
            if "异常" not in self.message and "队列已满" not in self.message:
                self.message = (
                    f"采集完成 {self.pages_captured} 页 "
                    f"(待处理 {capture_queue.pending_count(self.book)})。"
                    "可点「处理队列」开始 OCR/翻译。"
                )

    async def _worker_loop(self) -> None:
        """慢路径: 消费本地队列, OCR + 翻译。与采集完全独立。"""
        empty_idle = 0
        self._worker_stop = False
        try:
            while True:
                if getattr(self, "_worker_stop", False):
                    break
                job = await run_in_threadpool(capture_queue.next_pending, self.book)
                if job is None:
                    empty_idle += 1
                    # 采集仍在进行: 继续等新截图; 否则空转几次后结束
                    if self.capturing:
                        empty_idle = 0
                        await asyncio.sleep(0.4)
                        continue
                    if empty_idle >= 5:
                        break
                    await asyncio.sleep(0.4)
                    continue
                empty_idle = 0
                self.message = (
                    f"处理第 {job.index} 页 "
                    f"(待处理 {capture_queue.pending_count(self.book)})…"
                )
                try:
                    img = await run_in_threadpool(capture_queue.load_image_bytes, job)
                    if job.kind == "text" and (job.text or "").strip():
                        use_cap = {
                            "kind": "text",
                            "text": job.text,
                            "url": job.url,
                            "screenshot_bytes": None,
                        }
                    else:
                        use_cap = {
                            "kind": "image",
                            "text": "",
                            "url": job.url,
                            "screenshot_bytes": img,
                        }
                    from . import ocr as ocr_mod

                    # 图片页: 先 OCR → sidecar → 质量门槛 → 再翻译
                    if use_cap["kind"] == "image":
                        if not img:
                            await run_in_threadpool(
                                capture_queue.mark_done, job, "missing_image"
                            )
                            continue
                        ocr_text = await run_in_threadpool(
                            ocr_mod.read_page,
                            img,
                            self.source_lang,
                            self.direction == "rtl",
                        )
                        await run_in_threadpool(
                            _write_ocr_sidecar, self.book, job.index, ocr_text
                        )
                        if not ocr_mod.ocr_quality_ok(ocr_text or ""):
                            await run_in_threadpool(
                                capture_queue.mark_done, job, "ocr_quality"
                            )
                            logger.warning(
                                "第 %d 页 OCR 质量不足, 保留截图: %r",
                                job.index,
                                (ocr_text or "")[:40],
                            )
                            self.message = (
                                f"第 {job.index} 页 OCR 失败已跳过 "
                                f"(待处理 {capture_queue.pending_count(self.book)})"
                            )
                            continue
                        use_cap = {
                            "kind": "text",
                            "text": ocr_text,
                            "url": job.url,
                            "screenshot_bytes": None,
                        }

                    out = await self._translate_capture(use_cap, job.index)
                    orig = (out.get("orig") or "").strip()
                    if not orig:
                        await run_in_threadpool(
                            capture_queue.mark_done, job, "empty_ocr"
                        )
                        logger.warning("第 %d 页文本为空", job.index)
                    else:
                        await run_in_threadpool(capture_queue.mark_done, job, None)
                        self.message = (
                            f"已翻译第 {out['index']} 页 "
                            f"(待处理 {capture_queue.pending_count(self.book)})"
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("处理队列任务失败 index=%s", job.index)
                    await run_in_threadpool(capture_queue.mark_done, job, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("消费循环异常")
            self.message = f"翻译异常停止: {exc}"
        finally:
            self.working = False
            if not self.capturing:
                self.running = False
            if "异常" not in self.message:
                self.message = (
                    f"处理结束: 截图 {self.pages_captured} 页, "
                    f"已译 {self.pages_done} 页, "
                    f"队列剩余 {capture_queue.pending_count(self.book)}"
                )

    def stop(self) -> dict:
        """停采集 / 停处理。有采集先停采集; 再点停处理。"""
        if self.capturing:
            self.capturing = False
            self.message = (
                "已停止采集。"
                + (" 处理队列仍在运行…" if self.working else " 可点「处理队列」翻译已截图。")
            )
            return self.progress()
        if self.working:
            self._worker_stop = True
            self.message = "正在停止处理…"
            return self.progress()
        self.running = False
        return self.progress()

    def progress(self) -> dict:
        st = capture_queue.stats(self.book)
        return {
            "running": self.running or self.capturing or self.working,
            "capturing": self.capturing,
            "working": self.working,
            "book": self.book,
            "index": self.index,
            "pages_captured": self.pages_captured,
            "pages_done": self.pages_done,
            "queue_pending": st["pending"],
            "queue_mb": st["mb"],
            "queue_max_pending": st["max_pending"],
            "queue_max_mb": st["max_mb"],
            "existing_count": st.get("existing_count", 0),
            "next_append_index": st.get("next_append_index", self.index),
            "message": self.message,
            "last_orig": self.last_orig,
            "last_translated": self.last_translated,
        }


live = LiveSession()
