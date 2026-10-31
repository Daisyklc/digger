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

import os

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .browser_controller import controller
from .config import ENGINE_VERSION, settings
from .glossary import GlossaryStore
from .live_session import live
from .manga_pipeline import MangaPipeline
from .novel_session import NovelSession
from .schemas import (
    GlossaryTerm,
    HealthResponse,
    MangaTranslateResponse,
    NovelPageResponse,
    NovelSessionInfo,
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

# 本地个人工具: 允许扩展页面 / 浏览器直连 (生产可收紧白名单)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_text_translator = TextTranslator()
_glossary = GlossaryStore(settings.glossary_dir)
_novel = NovelSession()
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
        style=req.style,
        polish=req.polish,
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


# ---- 小说 (Kindle 日文) 端点 -----------------------------------------------


@app.post("/novel/page", response_model=NovelPageResponse)
async def novel_page(
    book: str = Form(...),
    index: int = Form(...),
    target_lang: str = Form("zh-CN"),
    source_lang: str = Form("ja"),
    series: str = Form(""),
    text: str = Form(""),
    vertical: bool = Form(True),
    polish: bool = Form(True),
    image: UploadFile | None = File(None),
) -> NovelPageResponse:
    """翻译并存入会话一页。DOM 文本给 text; 竖排位图给 image, 二选一。"""
    if not settings.llm_ready:
        raise HTTPException(status_code=503, detail="缺少 DEEPSEEK_API_KEY, 服务未就绪")
    image_bytes = await image.read() if image is not None else None
    return _novel.translate_page(
        book=book,
        index=index,
        target_lang=target_lang,
        source_lang=source_lang,
        series=series or None,
        text=text or None,
        image_bytes=image_bytes,
        vertical=vertical,
        polish=polish,
    )


@app.get("/novel/session", response_model=NovelSessionInfo)
def novel_session_info(book: str) -> NovelSessionInfo:
    return _novel.info(book)


@app.get("/novel/export", response_class=PlainTextResponse)
def novel_export(book: str, format: str = "md", layout: str = "translated") -> str:
    """导出整本。format: txt|md; layout: translated|bilingual。"""
    return _novel.export(book, fmt=format, layout=layout)


# ---- 受控浏览器 (Playwright) 端点 ------------------------------------------


@app.post("/browser/launch")
async def browser_launch(payload: dict = Body(default={})) -> dict:
    """启动(或复用)受控浏览器, 可选导航到 url。首次请在打开的窗口里手动登录。"""
    return await controller.launch(url=payload.get("url"), headless=payload.get("headless"))


@app.post("/browser/goto")
async def browser_goto(payload: dict = Body(...)) -> dict:
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="缺少 url")
    return await controller.goto(url)


@app.get("/browser/status")
async def browser_status() -> dict:
    return await controller.status()


@app.post("/browser/next")
async def browser_next(payload: dict = Body(default={})) -> dict:
    await controller.next_page(payload.get("direction", "rtl"))
    return {"ok": True}


# ---- 实时会话 (手动 / 自动) 端点 -------------------------------------------


@app.post("/session/translate-current")
async def session_translate_current(cfg: dict = Body(default={})) -> dict:
    """手动模式: 翻译受控浏览器当前页一次。"""
    if not settings.llm_ready:
        raise HTTPException(status_code=503, detail="缺少 DEEPSEEK_API_KEY, 服务未就绪")
    try:
        return await live.translate_current(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/session/auto")
async def session_auto(cfg: dict = Body(default={})) -> dict:
    """自动模式: 后台连续翻页 + 翻译 + 存盘。"""
    if not settings.llm_ready:
        raise HTTPException(status_code=503, detail="缺少 DEEPSEEK_API_KEY, 服务未就绪")
    try:
        return await live.start_auto(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/session/stop")
def session_stop() -> dict:
    return live.stop()


@app.get("/session/progress")
def session_progress() -> dict:
    return live.progress()


# ---- 本地 Web UI (仪表盘) --------------------------------------------------

_WEBUI_DIR = os.path.join(os.path.dirname(__file__), "..", "webui")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_WEBUI_DIR, "index.html"))


if os.path.isdir(_WEBUI_DIR):
    app.mount("/static", StaticFiles(directory=_WEBUI_DIR), name="static")
