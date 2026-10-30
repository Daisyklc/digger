"""WebTranslator 后端 FastAPI 入口。

路由:
    GET  /healthz           就绪状态 (LLM / OCR / 缺失配置)
    POST /translate/text    批量文本翻译
    POST /translate/image   漫画图翻译 (multipart)
    GET  /glossary          读术语库
    POST /glossary          写/锁定术语

启动: uvicorn backend.server:app --reload
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .config import ENGINE_VERSION, settings
from .glossary import GlossaryStore
from .manga_pipeline import MangaPipeline
from .schemas import (
    GlossaryTerm,
    HealthResponse,
    MangaTranslateResponse,
    TextTranslateRequest,
    TextTranslateResponse,
)
from .text_translator import TextTranslator

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("WebTranslator")

app = FastAPI(title="WebTranslator", version=ENGINE_VERSION)

_text_translator = TextTranslator()
_glossary = GlossaryStore(settings.glossary_dir)
_manga: MangaPipeline | None = None  # 惰性初始化, 避免无漫画需求时加载 OCR 重依赖


def _get_manga() -> MangaPipeline:
    global _manga
    if _manga is None:
        _manga = MangaPipeline()
    return _manga


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(
        llm_ready=settings.llm_ready,
        ocr_backend=settings.ocr_backend,
        missing=settings.missing(),
        engine_version=ENGINE_VERSION,
    )


@app.post("/translate/text", response_model=TextTranslateResponse)
def translate_text(req: TextTranslateRequest) -> TextTranslateResponse:
    if not settings.llm_ready:
        raise HTTPException(status_code=503, detail="缺少 DEEPSEEK_API_KEY, 服务未就绪")
    segments, cached = _text_translator.translate_batch(
        req.segments,
        target_lang=req.target_lang,
        source_lang=req.source_lang,
        series=req.series,
        context=req.context,
    )
    return TextTranslateResponse(
        segments=segments, cached=cached, engine_version=ENGINE_VERSION
    )


@app.post("/translate/image", response_model=MangaTranslateResponse)
async def translate_image(
    image: UploadFile = File(...),
    meta: str = Form("{}"),
) -> MangaTranslateResponse:
    if not settings.llm_ready:
        raise HTTPException(status_code=503, detail="缺少 DEEPSEEK_API_KEY, 服务未就绪")
    try:
        m = json.loads(meta)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="meta 不是合法 JSON")
    data = await image.read()
    return _get_manga().process(
        data,
        target_lang=m.get("target_lang", settings.target_lang),
        series=m.get("series"),
        mode=m.get("mode", "overlay"),
    )


@app.get("/glossary")
def get_glossary(series: str) -> dict:
    terms = _glossary.load(series)
    return {"series": series, "terms": [t.model_dump() for t in terms]}


@app.post("/glossary")
def upsert_glossary(series: str, term: GlossaryTerm, target_lang: str = "zh-CN") -> dict:
    terms = _glossary.load(series)
    existing = {t.src: t for t in terms}
    existing[term.src] = term
    merged = list(existing.values())
    _glossary.save(series, merged, target_lang)
    return {"series": series, "count": len(merged)}
