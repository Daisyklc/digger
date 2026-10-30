"""漫画 OCR: 气泡/文本块检测 + 文字识别。可插拔后端。

统一输出: [{bbox, text, direction, confidence}]
后端选择由 settings.ocr_backend 决定:
    - manga-ocr : 日漫竖排识别强 (默认)
    - paddle    : PaddleOCR 多语通用
    - vision-llm: 直接用多模态 LLM 给出文本 + 框

本文件给出接口与可替换实现骨架; 具体模型加载在对应后端内惰性完成,
以免未启用时也强制依赖重型库。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import settings

logger = logging.getLogger("WebTranslator.ocr")


@dataclass
class OcrBlock:
    bbox: list[int]  # [x, y, w, h]
    text: str
    direction: str = "horizontal"  # horizontal | vertical
    confidence: float = 1.0


@dataclass
class OcrResult:
    blocks: list[OcrBlock] = field(default_factory=list)


class BaseOcr:
    """OCR 后端统一接口。"""

    def detect_and_read(self, image_bytes: bytes) -> OcrResult:  # pragma: no cover
        raise NotImplementedError


class MangaOcrBackend(BaseOcr):
    """基于 manga-ocr + 气泡检测。模型惰性加载。"""

    def __init__(self) -> None:
        self._reader = None

    def _ensure(self):
        if self._reader is None:
            # 惰性导入, 未启用漫画模式时不触发重依赖
            from manga_ocr import MangaOcr  # type: ignore

            self._reader = MangaOcr()
        return self._reader

    def detect_and_read(self, image_bytes: bytes) -> OcrResult:
        reader = self._ensure()
        blocks = self._detect_bubbles(image_bytes)
        for b in blocks:
            crop = self._crop(image_bytes, b.bbox)
            b.text = reader(crop)  # type: ignore[operator]
        return OcrResult(blocks=blocks)

    @staticmethod
    def _detect_bubbles(image_bytes: bytes) -> list[OcrBlock]:
        """气泡/文本块检测。

        TODO: 接入漫画气泡检测模型 (如 comic-text-detector)。
        当前骨架: 返回空, 由上层决定是否整图走 vision-llm 兜底。
        """
        return []

    @staticmethod
    def _crop(image_bytes: bytes, bbox: list[int]) -> "object":
        """按 bbox 裁剪, 返回 PIL.Image。"""
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        x, y, w, h = bbox
        return img.crop((x, y, x + w, y + h))


def get_ocr() -> BaseOcr:
    """按配置返回 OCR 后端实例。"""
    backend = settings.ocr_backend
    if backend == "manga-ocr":
        return MangaOcrBackend()
    # paddle / vision-llm 后端留作扩展
    logger.warning("OCR 后端 %s 未实现, 回退 manga-ocr", backend)
    return MangaOcrBackend()


def read_page(image_bytes: bytes, source_lang: str = "ja", vertical: bool = True) -> str:
    """整页 OCR: 按正确阅读顺序还原整页纯文本 (非按气泡)。

    用于 Kindle 日文小说竖排位图。竖排阅读顺序为: 每列自上而下, 列序自右向左。

    推荐后端: 多模态 vision-llm (对竖排与阅读顺序最稳) 或 PaddleOCR 竖排模式。
    默认剥离 ルビ (振假名); 低置信时返回已识别部分。

    TODO: 接入具体整页 OCR 后端; 当前为接口占位, 返回空串由上层决定兜底。
    """
    logger.info("read_page lang=%s vertical=%s bytes=%d", source_lang, vertical, len(image_bytes))
    return ""
