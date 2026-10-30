"""请求 / 响应数据模型 (Pydantic)。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Segment(BaseModel):
    """一个待翻译文本段。id 为扩展侧节点稳定指纹。"""

    id: str
    text: str


class TranslatedSegment(BaseModel):
    id: str
    text: str
    used_terms: list[str] = Field(default_factory=list)
    error: str | None = None


class TextTranslateRequest(BaseModel):
    target_lang: str = "zh-CN"
    source_lang: str = "auto"
    series: str | None = None
    context: list[str] = Field(default_factory=list)
    segments: list[Segment]


class TextTranslateResponse(BaseModel):
    segments: list[TranslatedSegment]
    cached: int = 0
    engine_version: str


class MangaLayer(BaseModel):
    """漫画单个气泡的译文图层。"""

    bbox: list[int]  # [x, y, w, h]
    text: str
    font_px: int
    orig: str | None = None
    needs_review: bool = False


class MangaTranslateResponse(BaseModel):
    image_id: str
    mode: str = "overlay"  # overlay | rendered
    layers: list[MangaLayer] = Field(default_factory=list)
    rendered_png_b64: str | None = None
    engine_version: str


class GlossaryTerm(BaseModel):
    src: str
    dst: str
    type: str = "term"  # person | place | term | ...
    locked: bool = False
    note: str | None = None


class HealthResponse(BaseModel):
    llm_ready: bool
    ocr_backend: str
    missing: list[str]
    engine_version: str
