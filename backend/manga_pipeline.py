"""漫画流水线: OCR -> 翻译 -> 擦字 -> 重排。

对单张漫画图完成全流程, 同一张图/同一话的所有气泡共享上下文与术语,
保证整页风格统一。
"""

from __future__ import annotations

import base64
import hashlib
import logging

from .ocr import get_ocr
from .schemas import MangaLayer, MangaTranslateResponse, Segment
from .text_translator import TextTranslator
from .typeset import fit_font_size, inpaint, render
from .config import ENGINE_VERSION

logger = logging.getLogger("WebTranslator.manga")


def _phash(image_bytes: bytes) -> str:
    """图片指纹 (当前用内容哈希; 可替换为感知哈希以命中近似重复图)。"""
    return "sha1:" + hashlib.sha1(image_bytes).hexdigest()


class MangaPipeline:
    def __init__(self) -> None:
        self.ocr = get_ocr()
        self.translator = TextTranslator()

    def process(
        self,
        image_bytes: bytes,
        target_lang: str,
        series: str | None = None,
        mode: str = "overlay",
    ) -> MangaTranslateResponse:
        image_id = _phash(image_bytes)

        # 1) OCR: 识别气泡文字
        ocr_result = self.ocr.detect_and_read(image_bytes)
        if not ocr_result.blocks:
            logger.info("未检测到文字块: %s", image_id)
            return MangaTranslateResponse(
                image_id=image_id, mode=mode, layers=[], engine_version=ENGINE_VERSION
            )

        # 2) 翻译: 同图气泡作为一个批次, 共享上下文与术语
        segments = [
            Segment(id=str(i), text=b.text) for i, b in enumerate(ocr_result.blocks)
        ]
        translated, _ = self.translator.translate_batch(
            segments, target_lang=target_lang, series=series
        )
        tr_by_id = {t.id: t for t in translated}

        # 3) 组装图层 + 自动字号
        layers: list[MangaLayer] = []
        for i, block in enumerate(ocr_result.blocks):
            tr = tr_by_id.get(str(i))
            text = tr.text if tr else block.text
            x, y, w, h = block.bbox
            layers.append(
                MangaLayer(
                    bbox=block.bbox,
                    text=text,
                    font_px=fit_font_size(text, w, h),
                    orig=block.text,
                    needs_review=block.confidence < 0.6,
                )
            )

        # 4) overlay 直接返回图层; rendered 则擦字 + 贴译文
        if mode == "rendered":
            boxes = [l.bbox for l in layers]
            erased = inpaint(image_bytes, boxes)
            png = render(erased, [l.model_dump() for l in layers])
            return MangaTranslateResponse(
                image_id=image_id,
                mode="rendered",
                layers=layers,
                rendered_png_b64=base64.b64encode(png).decode("ascii"),
                engine_version=ENGINE_VERSION,
            )

        return MangaTranslateResponse(
            image_id=image_id, mode="overlay", layers=layers, engine_version=ENGINE_VERSION
        )
