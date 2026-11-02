"""截图采集队列: 把「翻页截图」与「OCR / 翻译」解耦。

流程:
    采集循环快速: capture → enqueue(落盘) → 翻页
    OCR 阶段: 读图 → 写 .ocr.txt sidecar → 按策略删图
    翻译阶段: 读 sidecar → LLM 翻译落盘

每页独立状态:
    status      — 图片生命周期 pending | done | error | dropped
    ocr_status  — pending | doing | done | failed
    tr_status   — pending | doing | done | failed

定量丢弃 (防占盘):
    - capture_max_pending: 未处理任务上限, 满则采集侧反压等待
    - capture_max_mb: 截图目录总大小上限
    - 默认 OCR 成功后立即删除截图 (capture_keep_done=0)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import settings

logger = logging.getLogger("WebTranslator.capture")


def _cfg_int(env: str, local: str, default: int) -> int:
    from .config import _cfg

    raw = _cfg(env, local, str(default))
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


@dataclass
class CaptureJob:
    book: str
    index: int
    kind: str  # text | image
    fingerprint: str
    url: str = ""
    text: str = ""
    image_path: str | None = None
    status: str = "pending"  # pending | done | error | dropped (图片生命周期)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    done_at: float | None = None
    # 三段解耦: OCR 与翻译各自独立状态, 互不阻塞
    ocr_status: str = "pending"  # pending | doing | done | failed
    tr_status: str = "pending"  # pending | doing | done | failed

    @property
    def id(self) -> str:
        return f"{self.book}_{self.index:06d}"


def _safe_book(book: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in book)


class CaptureQueue:
    """按书名隔离的本地截图队列。"""

    def __init__(self) -> None:
        self.root = Path(
            os.environ.get("WT_CAPTURE_DIR")
            or getattr(settings, "capture_dir", None)
            or "./data/captures"
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_pending = _cfg_int("WT_CAPTURE_MAX_PENDING", "capture_max_pending", 800)
        self.max_mb = _cfg_int("WT_CAPTURE_MAX_MB", "capture_max_mb", 5120)
        self.keep_done = _cfg_int("WT_CAPTURE_KEEP_DONE", "capture_keep_done", 0)
        self._lock = threading.Lock()

    def _book_dir(self, book: str) -> Path:
        d = self.root / _safe_book(book)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _meta_path(self, book: str, index: int) -> Path:
        return self._book_dir(book) / f"{index:06d}.json"

    def _img_path(self, book: str, index: int) -> Path:
        return self._book_dir(book) / f"{index:06d}.png"

    def _manifest_path(self, book: str) -> Path:
        return self._book_dir(book) / "manifest.json"

    def ocr_sidecar_path(self, book: str, index: int) -> Path:
        return self._book_dir(book) / f"{index:06d}.ocr.txt"

    def list_books(self) -> list[dict[str, Any]]:
        """下列表: captures 下各采集子目录 + 各阶段计数。"""
        with self._lock:
            books: list[dict[str, Any]] = []
            if not self.root.is_dir():
                return books
            for d in sorted(self.root.iterdir()):
                if not d.is_dir():
                    continue
                name = d.name
                st = self._stage_counts_unlocked(name)
                books.append(
                    {
                        "book": name,
                        "existing_count": st["total"],
                        "ocr_pending": st["ocr_pending"],
                        "ocr_done": st["ocr_done"],
                        "tr_pending": st["tr_pending"],
                        "tr_done": st["tr_done"],
                        "next_append_index": self.next_append_index(name),
                    }
                )
            return books

    def max_index(self, book: str) -> int | None:
        """已有截图/元数据的最大 index; 无则 None。"""
        d = self._book_dir(book)
        found: list[int] = []
        for f in d.glob("*.json"):
            if f.name == "manifest.json":
                continue
            try:
                found.append(int(f.stem))
            except ValueError:
                continue
        for f in d.glob("*.png"):
            try:
                found.append(int(f.stem))
            except ValueError:
                continue
        return max(found) if found else None

    def next_append_index(self, book: str) -> int:
        """只追加: 下一笔写入的 index = max+1 (或 0)。"""
        m = self.max_index(book)
        return 0 if m is None else m + 1

    def existing_count(self, book: str) -> int:
        """已落盘页数 (按 meta 计, 无 meta 则按 png)。"""
        d = self._book_dir(book)
        metas = [f for f in d.glob("*.json") if f.name != "manifest.json"]
        if metas:
            return len(metas)
        return len(list(d.glob("*.png")))

    def write_manifest(self, book: str, **extra: Any) -> None:
        """更新断点清单 (next_index 等), 供重启后续采。"""
        payload = {
            "book": book,
            "next_index": self.next_append_index(book),
            "existing_count": self.existing_count(book),
            "updated_at": time.time(),
            **extra,
        }
        try:
            self._manifest_path(book).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("写 manifest 失败: %s", exc)

    def _normalize_meta(self, m: dict) -> dict:
        """兼容旧 meta: 融合 status → 拆成 ocr_status / tr_status。"""
        if "ocr_status" in m and "tr_status" in m:
            return m
        st = m.get("status", "pending")
        err = (m.get("error") or "").lower()
        book = str(m.get("book") or "")
        index = int(m.get("index") or 0)
        sidecar = self.ocr_sidecar_path(book, index) if book else None
        has_sidecar = bool(sidecar and sidecar.is_file() and sidecar.stat().st_size > 0)

        if st == "done":
            m.setdefault("ocr_status", "done")
            m.setdefault("tr_status", "done")
        elif st == "dropped":
            m.setdefault("ocr_status", "failed")
            m.setdefault("tr_status", "pending")
        elif st == "error":
            if any(k in err for k in ("ocr", "missing_image", "empty")):
                m.setdefault("ocr_status", "failed")
                m.setdefault("tr_status", "pending")
            else:
                # 可能 OCR 已过、翻译失败; 有 sidecar 则 OCR 视为完成
                m.setdefault("ocr_status", "done" if has_sidecar else "failed")
                m.setdefault("tr_status", "failed" if has_sidecar else "pending")
        else:
            if has_sidecar:
                m.setdefault("ocr_status", "done")
            else:
                m.setdefault("ocr_status", "pending")
            m.setdefault("tr_status", "pending")
        return m

    def _job_from_meta(self, m: dict, book: str | None = None) -> CaptureJob:
        m = self._normalize_meta(dict(m))
        return CaptureJob(
            book=str(m.get("book") or book or "untitled"),
            index=int(m.get("index") or 0),
            kind=str(m.get("kind") or "image"),
            fingerprint=str(m.get("fingerprint") or ""),
            url=str(m.get("url") or ""),
            text=str(m.get("text") or ""),
            image_path=m.get("image_path"),
            status=str(m.get("status") or "pending"),
            error=m.get("error"),
            created_at=float(m.get("created_at") or time.time()),
            done_at=m.get("done_at"),
            ocr_status=str(m.get("ocr_status") or "pending"),
            tr_status=str(m.get("tr_status") or "pending"),
        )

    def _write_job(self, job: CaptureJob) -> None:
        meta = self._meta_path(job.book, job.index)
        meta.write_text(
            json.dumps(asdict(job), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def stats(self, book: str | None = None) -> dict[str, Any]:
        with self._lock:
            pending = done = error = 0
            bytes_total = 0
            for meta in self._iter_metas(book):
                st = meta.get("status", "pending")
                if st == "pending":
                    pending += 1
                elif st == "done":
                    done += 1
                elif st == "error":
                    error += 1
                img = meta.get("image_path")
                if img and os.path.isfile(img):
                    bytes_total += os.path.getsize(img)
            nxt = self.next_append_index(book) if book else 0
            stage = self._stage_counts_unlocked(book) if book else {}
            return {
                "pending": pending,
                "done": done,
                "error": error,
                "bytes": bytes_total,
                "mb": round(bytes_total / (1024 * 1024), 2),
                "max_pending": self.max_pending,
                "max_mb": self.max_mb,
                "existing_count": self.existing_count(book) if book else 0,
                "next_append_index": nxt,
                **stage,
            }

    def _stage_counts_unlocked(self, book: str | None) -> dict[str, int]:
        ocr_pending = ocr_doing = ocr_done = ocr_failed = 0
        tr_pending = tr_doing = tr_done = tr_failed = 0
        total = 0
        for meta in self._iter_metas(book):
            m = self._normalize_meta(dict(meta))
            total += 1
            os_ = m.get("ocr_status", "pending")
            ts = m.get("tr_status", "pending")
            if os_ == "pending":
                ocr_pending += 1
            elif os_ == "doing":
                ocr_doing += 1
            elif os_ == "done":
                ocr_done += 1
            elif os_ == "failed":
                ocr_failed += 1
            if ts == "pending":
                tr_pending += 1
            elif ts == "doing":
                tr_doing += 1
            elif ts == "done":
                tr_done += 1
            elif ts == "failed":
                tr_failed += 1
        return {
            "total": total,
            "ocr_pending": ocr_pending + ocr_failed,  # 可重试 failed
            "ocr_doing": ocr_doing,
            "ocr_done": ocr_done,
            "ocr_failed": ocr_failed,
            "tr_pending": tr_pending + tr_failed,
            "tr_doing": tr_doing,
            "tr_done": tr_done,
            "tr_failed": tr_failed,
        }

    def stage_stats(self, book: str) -> dict[str, Any]:
        with self._lock:
            return self._stage_counts_unlocked(book)

    def _iter_metas(self, book: str | None = None) -> list[dict]:
        roots = [self._book_dir(book)] if book else [
            p for p in self.root.iterdir() if p.is_dir()
        ]
        out: list[dict] = []
        for d in roots:
            for f in sorted(d.glob("*.json")):
                if f.name == "manifest.json":
                    continue
                try:
                    out.append(json.loads(f.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return out

    def pending_count(self, book: str) -> int:
        """兼容旧逻辑: 图片尚未消费完 (status=pending)。"""
        return sum(1 for m in self._iter_metas(book) if m.get("status") == "pending")

    def wait_for_slot(self, book: str, timeout: float = 120.0) -> bool:
        """采集反压: 待处理过多时等待消费端腾出空位。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                self._enforce_quota(book)
                if self.pending_count(book) < self.max_pending:
                    return True
            time.sleep(0.4)
        return False

    def enqueue(self, book: str, index: int, cap: dict) -> CaptureJob:
        """落盘截图 + 元数据。只追加: 若该 index 已存在则拒绝覆盖。"""
        with self._lock:
            self._enforce_quota(book)
            meta = self._meta_path(book, index)
            ip = self._img_path(book, index)
            if meta.exists() or ip.exists():
                raise FileExistsError(
                    f"拒绝覆盖已有采集 book={book} index={index}; "
                    f"请使用 next_append_index={self.next_append_index(book)}"
                )
            job = CaptureJob(
                book=book,
                index=index,
                kind=cap.get("kind") or "image",
                fingerprint=cap.get("fingerprint") or "",
                url=cap.get("url") or "",
                text=cap.get("text") or "",
                status="pending",
                ocr_status="pending",
                tr_status="pending",
            )
            img_bytes = cap.get("screenshot_bytes") or b""
            if img_bytes:
                with open(ip, "xb") as f:
                    f.write(img_bytes)
                job.image_path = str(ip)
            self._write_job(job)
            self.write_manifest(book, last_fingerprint=job.fingerprint)
            self._enforce_quota(book)
            logger.info(
                "入队 book=%s index=%d kind=%s pending=%d",
                book,
                index,
                job.kind,
                self.pending_count(book),
            )
            return job

    def reset_stale_doing(self, book: str, stage: str) -> int:
        """把上次异常中断留下的 doing 恢复为 pending。"""
        with self._lock:
            n = 0
            field = "ocr_status" if stage == "ocr" else "tr_status"
            for m in self._iter_metas(book):
                m = self._normalize_meta(dict(m))
                if m.get(field) != "doing":
                    continue
                m[field] = "pending"
                try:
                    self._meta_path(book, int(m["index"])).write_text(
                        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    n += 1
                except OSError:
                    continue
            return n

    def _has_ocr_input(self, m: dict) -> bool:
        if (m.get("kind") or "image") == "text" and (m.get("text") or "").strip():
            return True
        img = m.get("image_path")
        return bool(img and os.path.isfile(img))

    def claim_next_ocr(self, book: str) -> CaptureJob | None:
        """原子领取下一页 OCR 任务 (pending/failed → doing)。"""
        with self._lock:
            metas = [self._normalize_meta(dict(m)) for m in self._iter_metas(book)]
            metas.sort(key=lambda m: int(m.get("index", 0)))
            for m in metas:
                if m.get("status") == "dropped":
                    continue
                if m.get("ocr_status") not in ("pending", "failed"):
                    continue
                if not self._has_ocr_input(m):
                    # 无图无文: 直接标失败
                    m["ocr_status"] = "failed"
                    m["status"] = "error"
                    m["error"] = "missing_image"
                    m["done_at"] = time.time()
                    try:
                        self._meta_path(book, int(m["index"])).write_text(
                            json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    except OSError:
                        pass
                    continue
                m["ocr_status"] = "doing"
                m["error"] = None
                try:
                    self._meta_path(book, int(m["index"])).write_text(
                        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except OSError:
                    continue
                return self._job_from_meta(m, book)
            return None

    def finish_ocr(
        self, job: CaptureJob, text: str | None = None, error: str | None = None
    ) -> None:
        """OCR 完成: 写 sidecar; 成功则可按策略删图。"""
        with self._lock:
            if error:
                job.ocr_status = "failed"
                job.status = "error"
                job.error = error
                job.done_at = time.time()
                self._write_job(job)
                return

            content = (text or job.text or "").strip()
            side = self.ocr_sidecar_path(job.book, job.index)
            try:
                side.write_text(content, encoding="utf-8")
            except OSError as exc:
                job.ocr_status = "failed"
                job.status = "error"
                job.error = f"sidecar_write:{exc}"
                job.done_at = time.time()
                self._write_job(job)
                return

            job.ocr_status = "done"
            job.error = None
            # 图片生命周期: OCR 成功后可删图 (翻译读 sidecar)
            if self.keep_done <= 0:
                self._delete_image(job)
                job.status = "done"
                job.done_at = time.time()
            else:
                job.status = "pending"
            self._write_job(job)
            self._enforce_quota(job.book)

    def read_ocr_text(self, job: CaptureJob) -> str | None:
        side = self.ocr_sidecar_path(job.book, job.index)
        if side.is_file():
            try:
                return side.read_text(encoding="utf-8")
            except OSError:
                pass
        if (job.text or "").strip():
            return job.text
        return None

    def claim_next_translate(self, book: str) -> CaptureJob | None:
        """原子领取下一页翻译任务 (OCR 已完成且 tr pending/failed)。"""
        with self._lock:
            metas = [self._normalize_meta(dict(m)) for m in self._iter_metas(book)]
            metas.sort(key=lambda m: int(m.get("index", 0)))
            for m in metas:
                if m.get("ocr_status") != "done":
                    continue
                if m.get("tr_status") not in ("pending", "failed"):
                    continue
                job = self._job_from_meta(m, book)
                text = self.read_ocr_text(job)
                if not (text or "").strip():
                    m["tr_status"] = "failed"
                    m["error"] = "empty_ocr"
                    try:
                        self._meta_path(book, int(m["index"])).write_text(
                            json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    except OSError:
                        pass
                    continue
                m["tr_status"] = "doing"
                m["error"] = None
                try:
                    self._meta_path(book, int(m["index"])).write_text(
                        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except OSError:
                    continue
                return self._job_from_meta(m, book)
            return None

    def finish_translate(self, job: CaptureJob, error: str | None = None) -> None:
        with self._lock:
            if error:
                job.tr_status = "failed"
                job.error = error
            else:
                job.tr_status = "done"
                job.error = None
                job.done_at = time.time()
                if job.status == "pending":
                    job.status = "done"
            self._write_job(job)

    def next_pending(self, book: str) -> CaptureJob | None:
        """兼容旧消费端: 按 status=pending 领取。"""
        with self._lock:
            pending = [
                m for m in self._iter_metas(book) if m.get("status") == "pending"
            ]
            if not pending:
                return None
            pending.sort(key=lambda m: int(m.get("index", 0)))
            return self._job_from_meta(pending[0], book)

    def mark_done(self, job: CaptureJob, error: str | None = None) -> None:
        """兼容旧融合处理: 一次标完 OCR+翻译。"""
        with self._lock:
            if error:
                job.status = "error"
                job.error = error
                err_l = error.lower()
                if any(k in err_l for k in ("ocr", "missing_image", "empty")):
                    job.ocr_status = "failed"
                else:
                    if job.ocr_status != "done":
                        job.ocr_status = "done"
                    job.tr_status = "failed"
            else:
                job.status = "done"
                job.error = None
                job.ocr_status = "done"
                job.tr_status = "done"
            job.done_at = time.time()
            self._write_job(job)
            if error is None and self.keep_done <= 0:
                self._delete_image(job)
            self._enforce_quota(job.book)

    def requeue_failed(self, book: str, stage: str = "both") -> int:
        """把失败任务重新标为 pending。

        stage: ocr | translate | both
        """
        with self._lock:
            n = 0
            for m in self._iter_metas(book):
                m = self._normalize_meta(dict(m))
                changed = False
                if stage in ("ocr", "both") and m.get("ocr_status") == "failed":
                    img = m.get("image_path")
                    has_img = bool(img and os.path.isfile(img))
                    has_text = (m.get("kind") == "text") and (m.get("text") or "").strip()
                    if has_img or has_text:
                        m["ocr_status"] = "pending"
                        m["status"] = "pending"
                        m["error"] = None
                        m["done_at"] = None
                        changed = True
                if stage in ("translate", "both") and m.get("tr_status") == "failed":
                    if m.get("ocr_status") == "done":
                        m["tr_status"] = "pending"
                        m["error"] = None
                        changed = True
                # 旧融合 error + 有图
                if (
                    stage in ("ocr", "both")
                    and m.get("status") == "error"
                    and m.get("ocr_status") not in ("done", "pending")
                ):
                    img = m.get("image_path")
                    if img and os.path.isfile(img):
                        m["status"] = "pending"
                        m["ocr_status"] = "pending"
                        m["error"] = None
                        m["done_at"] = None
                        changed = True
                if not changed:
                    continue
                try:
                    self._meta_path(book, int(m["index"])).write_text(
                        json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    n += 1
                except OSError:
                    continue
            return n

    def load_image_bytes(self, job: CaptureJob) -> bytes | None:
        if not job.image_path or not os.path.isfile(job.image_path):
            return None
        try:
            return Path(job.image_path).read_bytes()
        except OSError:
            return None

    def _delete_image(self, job: CaptureJob) -> None:
        if job.image_path and os.path.isfile(job.image_path):
            try:
                os.remove(job.image_path)
            except OSError:
                pass
            job.image_path = None
            try:
                self._write_job(job)
            except OSError:
                pass

    def _dir_bytes(self, book: str | None = None) -> int:
        total = 0
        roots = [self._book_dir(book)] if book else [
            p for p in self.root.iterdir() if p.is_dir()
        ]
        for d in roots:
            for f in d.glob("*.png"):
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    def _enforce_quota(self, book: str) -> None:
        """定量丢弃: 先删多余已完成截图, 再必要时丢最旧 pending。"""
        metas = self._iter_metas(book)
        done = [m for m in metas if m.get("status") == "done"]
        if len(done) > self.keep_done:
            for m in done[: len(done) - self.keep_done]:
                self._drop_image_only(m)

        max_bytes = self.max_mb * 1024 * 1024
        while self._dir_bytes(book) > max_bytes:
            metas = self._iter_metas(book)
            done = [m for m in metas if m.get("status") == "done" and m.get("image_path")]
            if done:
                self._drop_image_only(done[0])
                continue
            pending = [m for m in metas if m.get("status") == "pending"]
            if not pending:
                break
            victim = pending[0]
            logger.warning(
                "截图配额已满, 丢弃未处理页 book=%s index=%s",
                book,
                victim.get("index"),
            )
            self._mark_dropped(victim)

        while True:
            pending = [m for m in self._iter_metas(book) if m.get("status") == "pending"]
            if len(pending) <= self.max_pending:
                break
            victim = pending[0]
            logger.warning(
                "待处理队列超限(%d), 丢弃 index=%s",
                self.max_pending,
                victim.get("index"),
            )
            self._mark_dropped(victim)

    def _drop_image_only(self, meta: dict) -> None:
        img = meta.get("image_path")
        if img and os.path.isfile(img):
            try:
                os.remove(img)
            except OSError:
                pass
        meta["image_path"] = None
        book = meta["book"]
        index = int(meta["index"])
        try:
            self._meta_path(book, index).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def _mark_dropped(self, meta: dict) -> None:
        self._drop_image_only(meta)
        meta["status"] = "dropped"
        meta["ocr_status"] = "failed"
        meta["error"] = "quota_exceeded"
        meta["done_at"] = time.time()
        try:
            self._meta_path(meta["book"], int(meta["index"])).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass


capture_queue = CaptureQueue()
