"""截图采集队列: 把「翻页截图」与「OCR/翻译」解耦。

流程:
    采集循环快速: capture → enqueue(落盘) → 翻页
    消费循环慢速: dequeue → OCR/翻译 → 标记完成 → 按策略删除截图

定量丢弃 (防占盘):
    - capture_max_pending: 未处理任务上限, 满则采集侧反压等待
    - capture_max_mb: 截图目录总大小上限, 超限删最旧已完成, 仍不够则删最旧待处理
    - 默认处理成功后立即删除截图 (capture_keep_done=0)
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
    status: str = "pending"  # pending | done | error | dropped
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    done_at: float | None = None

    @property
    def id(self) -> str:
        return f"{self.book}_{self.index:06d}"


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
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in book)
        d = self.root / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _meta_path(self, book: str, index: int) -> Path:
        return self._book_dir(book) / f"{index:06d}.json"

    def _img_path(self, book: str, index: int) -> Path:
        return self._book_dir(book) / f"{index:06d}.png"

    def _manifest_path(self, book: str) -> Path:
        return self._book_dir(book) / "manifest.json"

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
            }

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
            )
            img_bytes = cap.get("screenshot_bytes") or b""
            # 文本页也保留截图作备份, 但若无图且有文本仍可入队
            if img_bytes:
                # xb: 排他创建, 防止竞态覆盖
                with open(ip, "xb") as f:
                    f.write(img_bytes)
                job.image_path = str(ip)
            meta.write_text(
                json.dumps(asdict(job), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
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

    def next_pending(self, book: str) -> CaptureJob | None:
        with self._lock:
            pending = [
                m for m in self._iter_metas(book) if m.get("status") == "pending"
            ]
            if not pending:
                return None
            pending.sort(key=lambda m: int(m.get("index", 0)))
            m = pending[0]
            return CaptureJob(
                book=str(m.get("book") or book),
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
            )

    def mark_done(self, job: CaptureJob, error: str | None = None) -> None:
        with self._lock:
            job.status = "error" if error else "done"
            job.error = error
            job.done_at = time.time()
            meta = self._meta_path(job.book, job.index)
            meta.write_text(
                json.dumps(asdict(job), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # 仅成功时按策略删图; 失败保留截图以便改进 OCR 后重试
            if error is None and self.keep_done <= 0:
                self._delete_image(job)
            self._enforce_quota(job.book)

    def requeue_failed(self, book: str) -> int:
        """把仍有截图的 error 任务重新标为 pending, 返回恢复数量。"""
        with self._lock:
            n = 0
            for m in self._iter_metas(book):
                if m.get("status") != "error":
                    continue
                img = m.get("image_path")
                if not img or not os.path.isfile(img):
                    continue
                m["status"] = "pending"
                m["error"] = None
                m["done_at"] = None
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
            meta = self._meta_path(job.book, job.index)
            try:
                meta.write_text(
                    json.dumps(asdict(job), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
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
        # 只保留最近 keep_done 个已完成的图
        if len(done) > self.keep_done:
            for m in done[: len(done) - self.keep_done]:
                self._drop_image_only(m)

        max_bytes = self.max_mb * 1024 * 1024
        # 超容量: 删最旧 done, 再删最旧 pending (标记 dropped)
        while self._dir_bytes(book) > max_bytes:
            metas = self._iter_metas(book)
            done = [m for m in metas if m.get("status") == "done" and m.get("image_path")]
            if done:
                self._drop_image_only(done[0])
                continue
            pending = [m for m in metas if m.get("status") == "pending"]
            if not pending:
                break
            # 丢最旧未处理 (极端反压)
            victim = pending[0]
            logger.warning(
                "截图配额已满, 丢弃未处理页 book=%s index=%s",
                book,
                victim.get("index"),
            )
            self._mark_dropped(victim)

        # pending 数量上限: 丢最旧
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
        meta["error"] = "quota_exceeded"
        meta["done_at"] = time.time()
        try:
            self._meta_path(meta["book"], int(meta["index"])).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass


capture_queue = CaptureQueue()
