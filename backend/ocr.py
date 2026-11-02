"""小说 / 漫画 OCR。

默认后端改为 PaddleOCR (日文 + 竖排后处理):
    检测框 → 右到左分列排序 → 剥离疑似振假名 → 质量门槛

可选:
    - manga-ocr : 漫画气泡 / 旧兜底
    - vision-llm: 多模态整页识读
"""

from __future__ import annotations

import io
import logging
import os
import threading
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from .config import settings
from .ocr_postprocess import (
    PostprocessResult,
    TextLine,
    box_from_points,
    order_vertical_text,
    quality_gate,
)

# Windows + Paddle 3.x 上 oneDNN 易触发 ConvertPirAttribute 错误
os.environ.setdefault("FLAGS_use_mkldnn", "0")

logger = logging.getLogger("WebTranslator.ocr")

_paddle = None
_paddle_lock = threading.Lock()
_paddle_loading = False
# 推理非线程安全: 并行 OCR worker 共用一把 predict 锁
_paddle_predict_lock = threading.Lock()

_manga_reader = None
_manga_lock = threading.Lock()
_manga_loading = False


def ocr_loading() -> bool:
    return bool(_paddle_loading or _manga_loading)


def ocr_quality_ok(text: str, *, min_chars: int = 40) -> bool:
    ok, _ = quality_gate(text, min_chars=min_chars)
    return ok


@dataclass
class OcrBlock:
    bbox: list[int]  # [x, y, w, h]
    text: str
    direction: str = "horizontal"
    confidence: float = 1.0


@dataclass
class OcrResult:
    blocks: list[OcrBlock] = field(default_factory=list)


def _get_paddle():
    """惰性加载 PaddleOCR 日文管线。"""
    global _paddle, _paddle_loading
    if _paddle is not None:
        return _paddle
    with _paddle_lock:
        if _paddle is not None:
            return _paddle
        from paddleocr import PaddleOCR

        _paddle_loading = True
        try:
            logger.info("正在加载 PaddleOCR (lang=japan, textline_orientation=on)…")
            # enable_mkldnn=False 规避 Win 上 oneDNN PIR 问题
            kwargs = dict(
                lang="japan",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=True,
                device="cpu",
                enable_mkldnn=False,
            )
            # 若本机有明确日文识别模型名则优先 (PaddleOCR 3.x 可选)
            model = os.environ.get("WT_PADDLE_REC_MODEL", "").strip()
            if model:
                kwargs["text_recognition_model_name"] = model
            try:
                _paddle = PaddleOCR(**kwargs)
            except TypeError:
                # 旧签名兼容
                kwargs.pop("enable_mkldnn", None)
                _paddle = PaddleOCR(**kwargs)
            logger.info("PaddleOCR 就绪")
        finally:
            _paddle_loading = False
        return _paddle


def _get_manga_ocr():
    global _manga_reader, _manga_loading
    if _manga_reader is not None:
        return _manga_reader
    with _manga_lock:
        if _manga_reader is not None:
            return _manga_reader
        from manga_ocr import MangaOcr  # type: ignore

        _manga_loading = True
        try:
            logger.info("正在加载 manga-ocr…")
            _manga_reader = MangaOcr()
            logger.info("manga-ocr 就绪")
        finally:
            _manga_loading = False
        return _manga_reader


def _crop_kindle_chrome(img: Image.Image) -> Image.Image:
    w, h = img.size
    top = int(h * 0.10)
    bottom = int(h * 0.88)
    left = int(w * 0.06)
    right = int(w * 0.94)
    if bottom - top < 100 or right - left < 100:
        return img
    return img.crop((left, top, right, bottom))


def _parse_paddle_result(res_obj) -> list[TextLine]:
    """兼容 PaddleOCR 3.x OCRResult / dict。"""
    data = None
    if hasattr(res_obj, "get") and res_obj.get("rec_texts") is not None:
        data = res_obj
    elif hasattr(res_obj, "json"):
        j = res_obj.json
        if isinstance(j, dict):
            data = j.get("res", j)
    if data is None and isinstance(res_obj, dict):
        data = res_obj.get("res", res_obj)

    if not data:
        return []

    texts = list(data.get("rec_texts") or [])
    scores_raw = data.get("rec_scores")
    scores = list(scores_raw) if scores_raw is not None else [1.0] * len(texts)

    boxes = data.get("rec_boxes")
    if boxes is None or len(boxes) == 0:
        boxes = data.get("dt_polys")
    if boxes is None or len(boxes) == 0:
        boxes = data.get("rec_polys")
    if boxes is None:
        boxes = []

    lines: list[TextLine] = []
    for i, text in enumerate(texts):
        t = (text or "").strip()
        if not t:
            continue
        box = boxes[i] if i < len(boxes) else None
        # rec_boxes: [x0,y0,x1,y1]; polys: [[x,y],...]
        if box is not None:
            try:
                flat = list(box)
                if len(flat) == 4 and not isinstance(flat[0], (list, tuple, np.ndarray)):
                    x0, y0, x1, y1 = map(float, flat)
                else:
                    x0, y0, x1, y1 = box_from_points(flat)
            except Exception:  # noqa: BLE001
                x0, y0, x1, y1 = 0.0, 0.0, 0.0, 0.0
        else:
            x0, y0, x1, y1 = 0.0, 0.0, 0.0, 0.0
        sc = float(scores[i]) if i < len(scores) else 1.0
        lines.append(TextLine(text=t, x0=x0, y0=y0, x1=x1, y1=y1, score=sc))
    return lines


def _read_with_paddle(image_bytes: bytes, vertical: bool = True) -> PostprocessResult:
    engine = _get_paddle()
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = _crop_kindle_chrome(img)
    # 限制长边, 加快检测
    max_side = 1800
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
    arr = np.asarray(img)

    with _paddle_predict_lock:
        raw = engine.predict(arr)
    if not raw:
        return PostprocessResult(text="", ok=False, reason="empty_predict")
    lines = _parse_paddle_result(raw[0])
    if not lines:
        return PostprocessResult(text="", ok=False, reason="no_lines")

    if vertical:
        return order_vertical_text(lines)

    # 横排: 上→下, 左→右
    ordered = sorted(lines, key=lambda ln: (round(ln.cy / 20), ln.cx))
    text = "\n".join(ln.text for ln in ordered)
    ok, reason = quality_gate(
        text,
        mean_score=float(np.mean([ln.score for ln in ordered])) if ordered else 0.0,
        line_count=len(ordered),
    )
    return PostprocessResult(
        text=text,
        lines_kept=len(ordered),
        mean_score=float(np.mean([ln.score for ln in ordered])) if ordered else 0.0,
        columns=1,
        ok=ok,
        reason=reason,
    )


def _read_with_manga_columns(image_bytes: bytes) -> str:
    """旧 manga-ocr 分列兜底。"""
    reader = _get_manga_ocr()
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = _crop_kindle_chrome(img)
    max_side = 1800
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
        w, h = img.size

    target_col = 56
    n_cols = max(8, min(28, w // target_col))
    col_w = w / n_cols
    parts: list[str] = []
    for i in range(n_cols):
        right = int(round(w - i * col_w))
        left = max(0, int(round(w - (i + 1) * col_w)))
        if right - left < 18:
            continue
        col = img.crop((left, 0, right, h))
        try:
            t = (reader(col) or "").strip()  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            continue
        if t and ocr_quality_ok(t, min_chars=2):
            # 单列短句仍收; 最终再过整页门槛
            if t not in ("それでも、", "それはそういうことで、"):
                parts.append(t)
    return "\n".join(parts).strip()


def _read_with_vision_llm(image_bytes: bytes, source_lang: str, vertical: bool) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        return ""
    if not settings.llm_ready:
        return ""
    import base64

    b64 = base64.b64encode(image_bytes).decode("ascii")
    orient = "竖排 (列自上而下, 列从右到左)" if vertical else "横排"
    prompt = (
        f"请识别这张小说书页图片中的全部正文。源语言倾向: {source_lang}, 排版: {orient}。"
        "只输出正文纯文本, 不要翻译, 不要解释, 不要页眉页脚/进度条。"
        "日文振假名(ルビ)只保留汉字正文。"
    )
    try:
        client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.base_url)
        resp = client.chat.completions.create(
            model=settings.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                    ],
                }
            ],
            temperature=0.1,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("vision-llm OCR 失败: %s", exc)
        return ""


def read_page(image_bytes: bytes, source_lang: str = "ja", vertical: bool = True) -> str:
    """整页 OCR: Paddle 竖排排序为主, manga-ocr / vision 可选兜底。"""
    backend = (settings.ocr_backend or "paddle").lower()
    logger.info(
        "read_page lang=%s vertical=%s bytes=%d backend=%s",
        source_lang,
        vertical,
        len(image_bytes or b""),
        backend,
    )
    if not image_bytes:
        return ""

    text = ""
    meta_reason = ""

    if backend == "vision-llm":
        text = _read_with_vision_llm(image_bytes, source_lang, vertical)
    elif backend == "manga-ocr":
        try:
            text = _read_with_manga_columns(image_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.exception("manga-ocr 失败: %s", exc)
    else:
        # 默认 paddle
        try:
            pp = _read_with_paddle(image_bytes, vertical=vertical)
            text = pp.text
            meta_reason = pp.reason
            logger.info(
                "paddle 后处理: ok=%s reason=%s chars=%d cols=%d ruby=%d score=%.3f",
                pp.ok,
                pp.reason,
                len(pp.text or ""),
                pp.columns,
                pp.lines_ruby,
                pp.mean_score,
            )
            if pp.ok:
                return pp.text
            # 未过门槛时尝试 manga 分列兜底
            logger.warning("Paddle 结果未过门槛 (%s), 尝试 manga-ocr 分列兜底", pp.reason)
            try:
                alt = _read_with_manga_columns(image_bytes)
                if ocr_quality_ok(alt):
                    logger.info("manga 兜底通过, chars=%d", len(alt))
                    return alt
            except Exception as exc:  # noqa: BLE001
                logger.warning("manga 兜底失败: %s", exc)
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.exception("PaddleOCR 失败, 回退 manga-ocr: %s", exc)
            try:
                text = _read_with_manga_columns(image_bytes)
            except Exception as exc2:  # noqa: BLE001
                logger.exception("manga-ocr 也失败: %s", exc2)
                return ""

    if text and ocr_quality_ok(text):
        logger.info("read_page 识别到 %d 字 (%s)", len(text), meta_reason or backend)
        return text
    logger.warning("read_page 未通过质量门槛: %r", (text or "")[:60])
    return ""


# ---- 漫画气泡接口 (保留) ----------------------------------------------------


class BaseOcr:
    def detect_and_read(self, image_bytes: bytes) -> OcrResult:  # pragma: no cover
        raise NotImplementedError


class MangaOcrBackend(BaseOcr):
    def detect_and_read(self, image_bytes: bytes) -> OcrResult:
        reader = _get_manga_ocr()
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        text = reader(img)  # type: ignore[operator]
        if text and text.strip():
            return OcrResult(
                blocks=[OcrBlock(bbox=[0, 0, img.width, img.height], text=text.strip())]
            )
        return OcrResult(blocks=[])


def get_ocr() -> BaseOcr:
    return MangaOcrBackend()
