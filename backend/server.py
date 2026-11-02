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

import asyncio
import json
import logging
import os

# 须在 import paddle 相关模块前设置, 避免 Windows oneDNN 异常
os.environ.setdefault("FLAGS_use_mkldnn", "0")

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .browser_controller import controller
from .capture_queue import capture_queue
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
from .stages import stages
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


@app.on_event("startup")
async def _preload_ocr() -> None:
    """后台预加载 manga-ocr, 避免点「自动翻页」后卡在首次模型下载。"""

    def _load() -> None:
        try:
            from . import ocr as ocr_mod

            ocr_mod._get_manga_ocr()
        except Exception:  # noqa: BLE001
            logger.exception("预加载 OCR 失败 (可稍后在首次识别时重试)")

    asyncio.get_running_loop().run_in_executor(None, _load)


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
    try:
        return await controller.launch(
            url=payload.get("url"), headless=payload.get("headless")
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("启动受控浏览器失败")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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


# ---- 实时会话 / 三阶段解耦 端点 -------------------------------------------


@app.post("/session/translate-current")
async def session_translate_current(cfg: dict = Body(default={})) -> dict:
    """手动模式: 翻译受控浏览器当前页一次。"""
    if not settings.llm_ready:
        raise HTTPException(status_code=503, detail="缺少 DEEPSEEK_API_KEY, 服务未就绪")
    try:
        return await live.translate_current(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/session/books")
def session_books() -> dict:
    """采集子目录列表, 供 UI 下拉框选择。"""
    return {"books": capture_queue.list_books(), "root": str(capture_queue.root)}


@app.get("/session/capture-info")
def session_capture_info(book: str = "untitled") -> dict:
    """续采预览: 已有页数与下一追加 index (不启动采集)。"""
    st = capture_queue.stats(book)
    return {
        "book": book,
        "existing_count": st.get("existing_count", 0),
        "next_append_index": st.get("next_append_index", 0),
        "max_index": capture_queue.max_index(book),
        "ocr_pending": st.get("ocr_pending", 0),
        "ocr_done": st.get("ocr_done", 0),
        "tr_pending": st.get("tr_pending", 0),
        "tr_done": st.get("tr_done", 0),
    }


@app.post("/session/capture")
async def session_capture(cfg: dict = Body(default={})) -> dict:
    """只采集: 截图落盘 + 翻页, 不跑 OCR/翻译 (可不依赖 API Key)。"""
    try:
        return await stages.start_capture(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/session/ocr")
async def session_ocr(cfg: dict = Body(default={})) -> dict:
    """只 OCR: 对选定采集文件夹做有界并行识别。"""
    try:
        return await stages.start_ocr(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/session/translate")
async def session_translate(cfg: dict = Body(default={})) -> dict:
    """只翻译: 对选定文件夹中已 OCR 页做有界并行翻译。"""
    if not settings.llm_ready:
        raise HTTPException(status_code=503, detail="缺少 DEEPSEEK_API_KEY, 服务未就绪")
    try:
        return await stages.start_translate(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/session/process")
async def session_process(cfg: dict = Body(default={})) -> dict:
    """兼容旧接口: OCR + 翻译。"""
    if not settings.llm_ready:
        raise HTTPException(status_code=503, detail="缺少 DEEPSEEK_API_KEY, 服务未就绪")
    try:
        return await stages.start_process(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/session/auto")
async def session_auto(cfg: dict = Body(default={})) -> dict:
    """全自动运行: 同一本书上采集 + OCR + 翻译并行。"""
    if not settings.llm_ready:
        raise HTTPException(status_code=503, detail="缺少 DEEPSEEK_API_KEY, 服务未就绪")
    try:
        return await stages.start_auto(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/session/stop")
def session_stop(payload: dict = Body(default={})) -> dict:
    """停止。可选 stage=capture|ocr|translate, book=..."""
    return stages.stop(
        stage=payload.get("stage"),
        book=payload.get("book"),
    )


@app.get("/session/progress")
def session_progress(book: str | None = None) -> dict:
    return stages.progress(book)


# ---- 本地 Web UI (仪表盘) --------------------------------------------------

_WEBUI_DIR = os.path.join(os.path.dirname(__file__), "..", "webui")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(_WEBUI_DIR, "index.html"))


if os.path.isdir(_WEBUI_DIR):
    app.mount("/static", StaticFiles(directory=_WEBUI_DIR), name="static")
