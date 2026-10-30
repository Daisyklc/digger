"""漫画贴字: 擦除原文 (inpaint) + 译文重排渲染。

两种产出:
    - overlay : 只返回图层 (bbox + 译文 + 字号), 由扩展侧叠加 (默认, 省带宽)
    - rendered: 返回整张擦字贴译文的 PNG

本文件提供:
    - fit_font_size : 二分拟合, 让译文恰好填充 bbox 不溢出
    - inpaint       : 擦除 bbox 内原文 (OpenCV)
    - render        : 在擦字图上绘制译文 (PIL)
"""

from __future__ import annotations

import io
import logging

logger = logging.getLogger("WebTranslator.typeset")

# 默认中文字体, 可由环境/配置覆盖
DEFAULT_FONT_PATH = "./assets/fonts/SourceHanSansSC.otf"


def fit_font_size(text: str, box_w: int, box_h: int, font_path: str = DEFAULT_FONT_PATH) -> int:
    """二分查找使 text 在 (box_w, box_h) 内排满而不溢出的最大字号。"""
    from PIL import ImageFont

    lo, hi, best = 8, max(box_h, 8), 8
    while lo <= hi:
        mid = (lo + hi) // 2
        try:
            font = ImageFont.truetype(font_path, mid)
        except OSError:
            # 无字体文件时给一个保守估计, 不阻断流程
            return max(12, min(box_h // 2, 28))
        lines = _wrap(text, font, box_w)
        total_h = len(lines) * (mid + 2)
        if total_h <= box_h:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _wrap(text: str, font, box_w: int) -> list[str]:
    """按像素宽度换行 (CJK 逐字, 其余按词)。"""
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        w = font.getlength(trial) if hasattr(font, "getlength") else len(trial) * 10
        if w > box_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def inpaint(image_bytes: bytes, boxes: list[list[int]]) -> bytes:
    """擦除给定 bbox 内的原文, 返回修复后的 PNG 字节。"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.warning("未安装 opencv, 跳过 inpaint")
        return image_bytes

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return image_bytes
    mask = np.zeros(img.shape[:2], dtype=np.uint8)
    for x, y, w, h in boxes:
        mask[y : y + h, x : x + w] = 255
    repaired = cv2.inpaint(img, mask, 3, cv2.INPAINT_TELEA)
    ok, buf = cv2.imencode(".png", repaired)
    return buf.tobytes() if ok else image_bytes


def render(image_bytes: bytes, layers: list[dict], font_path: str = DEFAULT_FONT_PATH) -> bytes:
    """在擦字图上绘制译文, 返回整张 PNG 字节。

    layers: [{bbox:[x,y,w,h], text, font_px}]
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    for layer in layers:
        x, y, w, h = layer["bbox"]
        size = layer.get("font_px") or fit_font_size(layer["text"], w, h, font_path)
        try:
            font = ImageFont.truetype(font_path, size)
        except OSError:
            font = ImageFont.load_default()
        lines = _wrap(layer["text"], font, w)
        ty = y
        for line in lines:
            # 白底黑字 + 描边提升可读性
            draw.text((x, ty), line, fill="black", font=font,
                      stroke_width=2, stroke_fill="white")
            ty += size + 2
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
