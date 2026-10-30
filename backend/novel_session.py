"""小说会话: 自动翻页保存模式的服务端状态 + 整本导出。

以 book_id 为命名空间, 逐页存 {index, orig, translated, polished} 落盘。
支持:
    - translate_page : 翻译并存入一页 (文本或整页竖排位图), 幂等去重
    - info           : 进度查询 (断点续传依据)
    - export         : 整本导出 txt / md (纯译文 / 双语对照)

跨页上下文: 自动携带上一页译文末尾若干字, 维持人称/语气跨页连贯。
"""

from __future__ import annotations

import json
import logging
import os
import re

from . import ocr
from .config import NOVEL_CONTEXT_CHARS, settings
from .schemas import NovelPageResponse, NovelSessionInfo, Segment
from .text_translator import TextTranslator

logger = logging.getLogger("WebTranslator.novel")


class NovelSession:
    def __init__(self) -> None:
        self.root = settings.novel_dir
        os.makedirs(self.root, exist_ok=True)
        self.translator = TextTranslator()

    # ---- 持久化 ------------------------------------------------------------
    def _path(self, book: str) -> str:
        safe = re.sub(r"[^\w\-]", "_", book)
        return os.path.join(self.root, f"{safe}.json")

    def _load(self, book: str) -> dict:
        path = self._path(book)
        if not os.path.exists(path):
            return {"book": book, "target_lang": settings.target_lang, "pages": {}}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"book": book, "target_lang": settings.target_lang, "pages": {}}

    def _save(self, book: str, data: dict) -> None:
        with open(self._path(book), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---- 核心: 翻译一页 ----------------------------------------------------
    def translate_page(
        self,
        book: str,
        index: int,
        target_lang: str,
        source_lang: str = "ja",
        series: str | None = None,
        text: str | None = None,
        image_bytes: bytes | None = None,
        vertical: bool = True,
        polish: bool = True,
    ) -> NovelPageResponse:
        data = self._load(book)
        data["target_lang"] = target_lang
        key = str(index)

        # 幂等: 已翻过直接返回 (断点续传)
        if key in data["pages"]:
            p = data["pages"][key]
            return NovelPageResponse(
                book=book, index=index, orig=p["orig"],
                translated=p["translated"], polished=p.get("polished"), cached=True,
            )

        # 取原文: DOM 文本直接用; 否则整页竖排 OCR
        orig = text if text is not None else ""
        if not orig and image_bytes is not None:
            orig = ocr.read_page(image_bytes, source_lang=source_lang, vertical=vertical)
        if not orig.strip():
            return NovelPageResponse(
                book=book, index=index, orig="", translated="", polished=None
            )

        # 跨页上下文: 上一页译文末尾
        context = []
        prev = data["pages"].get(str(index - 1))
        if prev:
            tail = (prev.get("polished") or prev.get("translated") or "")[-NOVEL_CONTEXT_CHARS:]
            if tail:
                context.append(tail)

        segments = [Segment(id="pg", text=orig)]
        translated_segs, _ = self.translator.translate_batch(
            segments,
            target_lang=target_lang,
            source_lang=source_lang,
            series=series,
            context=context,
            style="novel",
            polish=False,  # 润色单独走整页, 保证段落级连贯
        )
        translated = translated_segs[0].text

        polished = None
        if polish:
            polished = self.translator.polish_text(translated, target_lang)

        data["pages"][key] = {
            "index": index,
            "orig": orig,
            "translated": translated,
            "polished": polished,
        }
        self._save(book, data)
        return NovelPageResponse(
            book=book, index=index, orig=orig, translated=translated, polished=polished
        )

    # ---- 进度 --------------------------------------------------------------
    def info(self, book: str) -> NovelSessionInfo:
        data = self._load(book)
        pages = data["pages"]
        last = max((int(k) for k in pages), default=None)
        return NovelSessionInfo(
            book=book,
            pages_done=len(pages),
            last_index=last,
            target_lang=data.get("target_lang", settings.target_lang),
        )

    # ---- 导出 --------------------------------------------------------------
    def export(self, book: str, fmt: str = "md", layout: str = "translated") -> str:
        data = self._load(book)
        pages = sorted(data["pages"].values(), key=lambda p: p["index"])
        parts: list[str] = []
        for p in pages:
            body = p.get("polished") or p.get("translated") or ""
            if layout == "bilingual":
                if fmt == "md":
                    parts.append(f"> {p['orig']}\n\n{body}")
                else:
                    parts.append(f"【原文】{p['orig']}\n【译文】{body}")
            else:
                parts.append(body)
        sep = "\n\n---\n\n" if fmt == "md" else "\n\n"
        return sep.join(parts)
