"""三阶段编排: 采集 / OCR / 翻译 可独立选文件夹异步运行。

- capture : 委托 LiveSession 采集循环
- ocr     : 有界并行读图 → sidecar
- translate: 有界并行读 sidecar → LLM 落盘
- auto    : 同一本书上同时启动三阶段 (流水线互喂)

并行数默认 2, 上限 4 (照顾 Windows 低内存)。
Paddle 推理另有模块级锁, OCR 并行主要加速前后处理与 I/O。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.concurrency import run_in_threadpool

from .capture_queue import capture_queue

logger = logging.getLogger("WebTranslator.stages")

_MAX_CONCURRENCY = 4
_DEFAULT_CONCURRENCY = 2


def _clamp_concurrency(n: Any) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = _DEFAULT_CONCURRENCY
    return max(1, min(_MAX_CONCURRENCY, v))


class StageManager:
    def __init__(self) -> None:
        # (stage, book) → Task
        self._tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._stop: dict[tuple[str, str], bool] = {}
        self.concurrency = _DEFAULT_CONCURRENCY
        self.message = "就绪"
        self.last_orig = ""
        self.last_translated = ""
        self._cfg: dict[str, Any] = {}
        # 当前关注的书 (进度展示)
        self.focus_book = "untitled"

    def _key(self, stage: str, book: str) -> tuple[str, str]:
        return (stage, book)

    def is_running(self, stage: str, book: str | None = None) -> bool:
        if book is None:
            return any(
                k[0] == stage and not t.done() for k, t in self._tasks.items()
            )
        t = self._tasks.get(self._key(stage, book))
        return bool(t and not t.done())

    def any_running(self, book: str | None = None) -> bool:
        from .live_session import live

        if book is None:
            return (
                live.capturing
                or any(not t.done() for t in self._tasks.values())
            )
        return live.capturing and live.book == book or any(
            k[1] == book and not t.done() for k, t in self._tasks.items()
        )

    def _apply_cfg(self, cfg: dict) -> dict:
        self._cfg = dict(cfg or {})
        book = (self._cfg.get("book") or "untitled").strip() or "untitled"
        self.focus_book = book
        self._cfg["book"] = book
        self.concurrency = _clamp_concurrency(
            self._cfg.get("concurrency", self.concurrency)
        )
        self._cfg["concurrency"] = self.concurrency
        return self._cfg

    async def start_capture(self, cfg: dict) -> dict:
        from .live_session import live

        c = self._apply_cfg(cfg)
        await live.start_capture(c)
        self.message = live.message
        return self.progress(c["book"])

    async def start_ocr(self, cfg: dict) -> dict:
        c = self._apply_cfg(cfg)
        book = c["book"]
        key = self._key("ocr", book)
        if self.is_running("ocr", book):
            self.message = f"OCR 已在运行: {book}"
            return self.progress(book)

        revived = await run_in_threadpool(capture_queue.requeue_failed, book, "ocr")
        await run_in_threadpool(capture_queue.reset_stale_doing, book, "ocr")
        st = capture_queue.stage_stats(book)
        self._stop[key] = False
        self.message = (
            f"OCR 中…「{book}」待识别 {st.get('ocr_pending', 0)}"
            + (f", 重试 {revived}" if revived else "")
            + f", 并行 {self.concurrency}"
        )
        self._tasks[key] = asyncio.create_task(self._ocr_loop(book, c))
        return self.progress(book)

    async def start_translate(self, cfg: dict) -> dict:
        from .config import settings

        if not settings.llm_ready:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY, 服务未就绪")
        c = self._apply_cfg(cfg)
        book = c["book"]
        key = self._key("translate", book)
        if self.is_running("translate", book):
            self.message = f"翻译已在运行: {book}"
            return self.progress(book)

        revived = await run_in_threadpool(
            capture_queue.requeue_failed, book, "translate"
        )
        await run_in_threadpool(capture_queue.reset_stale_doing, book, "translate")
        st = capture_queue.stage_stats(book)
        self._stop[key] = False
        self.message = (
            f"翻译中…「{book}」待译 {st.get('tr_pending', 0)}"
            + (f", 重试 {revived}" if revived else "")
            + f", 并行 {self.concurrency}"
        )
        self._tasks[key] = asyncio.create_task(self._translate_loop(book, c))
        return self.progress(book)

    async def start_auto(self, cfg: dict) -> dict:
        """全自动: 同一本书上同时开采集 + OCR + 翻译。"""
        from .config import settings

        if not settings.llm_ready:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY, 服务未就绪")
        c = self._apply_cfg(cfg)
        await self.start_capture(c)
        await self.start_ocr(c)
        await self.start_translate(c)
        self.message = (
            f"全自动运行中…「{c['book']}」采集/OCR/翻译并行 "
            f"(并行数 {self.concurrency})"
        )
        return self.progress(c["book"])

    async def start_process(self, cfg: dict) -> dict:
        """兼容旧「处理队列」: OCR + 翻译。"""
        c = self._apply_cfg(cfg)
        await self.start_ocr(c)
        await self.start_translate(c)
        self.message = f"处理队列中…「{c['book']}」OCR+翻译"
        return self.progress(c["book"])

    def stop(self, stage: str | None = None, book: str | None = None) -> dict:
        """停阶段。无参: 先停采集, 再停当前焦点书的 OCR/翻译。"""
        from .live_session import live

        b = book or self.focus_book

        if stage == "capture" or (stage is None and live.capturing):
            live.capturing = False
            self.message = "已停止采集。" + (
                " OCR/翻译仍在运行…"
                if self.is_running("ocr", b) or self.is_running("translate", b)
                else ""
            )
            return self.progress(b)

        if stage in ("ocr", "translate"):
            key = self._key(stage, b)
            self._stop[key] = True
            self.message = f"正在停止{('OCR' if stage == 'ocr' else '翻译')}…"
            return self.progress(b)

        # 默认: 停焦点书全部后台阶段
        if self.is_running("ocr", b):
            self._stop[self._key("ocr", b)] = True
        if self.is_running("translate", b):
            self._stop[self._key("translate", b)] = True
        if live.capturing:
            live.capturing = False
        self.message = "正在停止…"
        return self.progress(b)

    async def _ocr_loop(self, book: str, cfg: dict) -> None:
        key = self._key("ocr", book)
        conc = _clamp_concurrency(cfg.get("concurrency", self.concurrency))
        sem = asyncio.Semaphore(conc)
        source_lang = cfg.get("source_lang") or "ja"
        vertical = (cfg.get("direction") or "rtl") == "rtl"
        idle = 0

        async def one() -> bool:
            """领取并处理一页; 返回是否领到任务。"""
            job = await run_in_threadpool(capture_queue.claim_next_ocr, book)
            if job is None:
                return False

            async with sem:
                try:
                    await self._ocr_one(job, source_lang, vertical)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("OCR 失败 index=%s", job.index)
                    await run_in_threadpool(
                        capture_queue.finish_ocr, job, None, str(exc)
                    )
            st = capture_queue.stage_stats(book)
            self.message = (
                f"OCR「{book}」第 {job.index} 页 "
                f"(待 {st.get('ocr_pending', 0)}, 完成 {st.get('ocr_done', 0)})"
            )
            return True

        try:
            while not self._stop.get(key):
                # 并发领取一批
                batch = await asyncio.gather(*[one() for _ in range(conc)])
                if any(batch):
                    idle = 0
                    continue
                # 无任务: 采集还在同书跑则继续等
                from .live_session import live

                if live.capturing and live.book == book:
                    idle = 0
                    await asyncio.sleep(0.5)
                    continue
                idle += 1
                if idle >= 6:
                    break
                await asyncio.sleep(0.4)
        except Exception as exc:  # noqa: BLE001
            logger.exception("OCR 循环异常")
            self.message = f"OCR 异常停止: {exc}"
        finally:
            self._tasks.pop(key, None)
            self._stop.pop(key, None)
            st = capture_queue.stage_stats(book)
            if "异常" not in self.message:
                self.message = (
                    f"OCR 结束「{book}」: 完成 {st.get('ocr_done', 0)}, "
                    f"失败 {st.get('ocr_failed', 0)}, "
                    f"剩余 {st.get('ocr_pending', 0)}"
                )

    async def _ocr_one(self, job, source_lang: str, vertical: bool) -> None:
        from . import ocr as ocr_mod

        # 文本页: 直接写 sidecar
        if job.kind == "text" and (job.text or "").strip():
            text = job.text
            if not ocr_mod.ocr_quality_ok(text):
                # 文本页放宽: 仍写入, 由翻译侧决定
                pass
            await run_in_threadpool(capture_queue.finish_ocr, job, text, None)
            return

        img = await run_in_threadpool(capture_queue.load_image_bytes, job)
        if not img:
            await run_in_threadpool(
                capture_queue.finish_ocr, job, None, "missing_image"
            )
            return

        ocr_text = await run_in_threadpool(
            ocr_mod.read_page, img, source_lang, vertical
        )
        if not ocr_mod.ocr_quality_ok(ocr_text or ""):
            # 仍写 sidecar 便于抽查, 但标失败不进翻译
            side = capture_queue.ocr_sidecar_path(job.book, job.index)
            try:
                side.write_text(ocr_text or "", encoding="utf-8")
            except OSError:
                pass
            await run_in_threadpool(
                capture_queue.finish_ocr, job, None, "ocr_quality"
            )
            logger.warning(
                "第 %d 页 OCR 质量不足: %r", job.index, (ocr_text or "")[:40]
            )
            return

        await run_in_threadpool(capture_queue.finish_ocr, job, ocr_text, None)

    async def _translate_loop(self, book: str, cfg: dict) -> None:
        key = self._key("translate", book)
        conc = _clamp_concurrency(cfg.get("concurrency", self.concurrency))
        sem = asyncio.Semaphore(conc)
        idle = 0

        async def one() -> bool:
            job = await run_in_threadpool(capture_queue.claim_next_translate, book)
            if job is None:
                return False
            async with sem:
                try:
                    await self._translate_one(job, cfg)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("翻译失败 index=%s", job.index)
                    await run_in_threadpool(
                        capture_queue.finish_translate, job, str(exc)
                    )
            st = capture_queue.stage_stats(book)
            self.message = (
                f"翻译「{book}」第 {job.index} 页 "
                f"(待 {st.get('tr_pending', 0)}, 完成 {st.get('tr_done', 0)})"
            )
            return True

        try:
            while not self._stop.get(key):
                batch = await asyncio.gather(*[one() for _ in range(conc)])
                if any(batch):
                    idle = 0
                    continue
                # OCR 或采集还在同书跑 → 等下游产出
                if (
                    self.is_running("ocr", book)
                    or self._capture_active(book)
                ):
                    idle = 0
                    await asyncio.sleep(0.5)
                    continue
                idle += 1
                if idle >= 6:
                    break
                await asyncio.sleep(0.4)
        except Exception as exc:  # noqa: BLE001
            logger.exception("翻译循环异常")
            self.message = f"翻译异常停止: {exc}"
        finally:
            self._tasks.pop(key, None)
            self._stop.pop(key, None)
            st = capture_queue.stage_stats(book)
            if "异常" not in self.message:
                self.message = (
                    f"翻译结束「{book}」: 完成 {st.get('tr_done', 0)}, "
                    f"失败 {st.get('tr_failed', 0)}, "
                    f"剩余 {st.get('tr_pending', 0)}"
                )

    def _capture_active(self, book: str) -> bool:
        from .live_session import live

        return bool(live.capturing and live.book == book)

    async def _translate_one(self, job, cfg: dict) -> None:
        from .live_session import live

        text = await run_in_threadpool(capture_queue.read_ocr_text, job)
        if not (text or "").strip():
            await run_in_threadpool(
                capture_queue.finish_translate, job, "empty_ocr"
            )
            return

        # 走 NovelSession (带跨页上下文 + 落盘)
        resp = await run_in_threadpool(
            live.novel.translate_page,
            book=job.book,
            index=job.index,
            target_lang=cfg.get("target_lang") or "zh-CN",
            source_lang=cfg.get("source_lang") or "ja",
            series=cfg.get("series") or None,
            text=text,
            image_bytes=None,
            vertical=(cfg.get("direction") or "rtl") == "rtl",
            polish=bool(cfg.get("polish", True)),
        )
        orig = (resp.orig or "").strip()
        if not orig:
            await run_in_threadpool(
                capture_queue.finish_translate, job, "empty_ocr"
            )
            return
        self.last_orig = resp.orig
        self.last_translated = resp.polished or resp.translated
        await run_in_threadpool(capture_queue.finish_translate, job, None)

    def progress(self, book: str | None = None) -> dict:
        from .live_session import live

        b = book or self.focus_book or live.book or "untitled"
        st = capture_queue.stats(b)
        ocr_run = self.is_running("ocr", b)
        tr_run = self.is_running("translate", b)
        capturing = bool(live.capturing and live.book == b)
        # 若焦点书没在采, 但全局在采别的书, 仍暴露 capturing 给旧 UI
        if live.capturing and not capturing and b == self.focus_book:
            capturing = True
        running = capturing or ocr_run or tr_run
        pages_done = 0
        try:
            pages_done = live.novel.info(b).pages_done
        except Exception:  # noqa: BLE001
            pages_done = st.get("tr_done", 0)

        msg = self.message
        if capturing and live.message:
            # 采集进行时优先展示采集消息
            if ocr_run or tr_run:
                msg = f"{live.message} | {self.message}"
            else:
                msg = live.message

        return {
            "running": running,
            "capturing": capturing or live.capturing,
            "working": ocr_run or tr_run,
            "ocr_running": ocr_run,
            "translate_running": tr_run,
            "book": b,
            "index": live.index if live.book == b else st.get("next_append_index", 0),
            "pages_captured": st.get("existing_count", 0),
            "pages_done": pages_done,
            "queue_pending": st.get("pending", 0),
            "queue_mb": st.get("mb", 0),
            "queue_max_pending": st.get("max_pending", 0),
            "queue_max_mb": st.get("max_mb", 0),
            "existing_count": st.get("existing_count", 0),
            "next_append_index": st.get("next_append_index", 0),
            "ocr_pending": st.get("ocr_pending", 0),
            "ocr_done": st.get("ocr_done", 0),
            "ocr_failed": st.get("ocr_failed", 0),
            "tr_pending": st.get("tr_pending", 0),
            "tr_done": st.get("tr_done", 0),
            "tr_failed": st.get("tr_failed", 0),
            "concurrency": self.concurrency,
            "message": msg,
            "last_orig": self.last_orig or live.last_orig,
            "last_translated": self.last_translated or live.last_translated,
            "stages": {
                "capture": live.capturing,
                "ocr": ocr_run,
                "translate": tr_run,
            },
        }


stages = StageManager()
