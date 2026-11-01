"""文本型翻译: 批量 + 上下文感知 + 术语一致。

流程: 去重/缓存 -> 术语注入 -> 分段打包 -> DeepSeek -> 逐段对齐校验 ->
      术语强制回填 -> 写缓存。
LLM 失败对单批兜底: 返回原文并标记 error, 不拖垮整页。
"""

from __future__ import annotations

import json
import logging
import time

from .cache import FileCache, make_key
from .config import MAX_SEGMENTS_PER_BATCH, settings
from .glossary import GlossaryStore
from .schemas import Segment, TranslatedSegment

logger = logging.getLogger("WebTranslator.text")

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore


_SYSTEM_PROMPT = (
    "你是专业的本地化译者。将给定 JSON 数组中每个 segment 的 text 翻译为目标语言。"
    "必须保持数组顺序与 id 一一对应, 不得合并、增删或改变 id。"
    "保留原文的标点风格与语气。只返回 JSON 数组, 形如 "
    '[{"id": "...", "text": "译文"}], 不要任何解释或代码块标记。'
)

# 小说场景: 文学直译, 强调人称/称谓/语气, 为后续润色留出余地
_NOVEL_SYSTEM_PROMPT = (
    "你是资深文学译者, 正在翻译小说正文。将给定 JSON 数组中每个 segment 的 text "
    "翻译为目标语言, 保持数组顺序与 id 一一对应, 不得合并、增删或改变 id。"
    "忠实保留原意、人物称谓、第一/第二/第三人称与语气, 不要漏译或自行脑补情节。"
    "只返回 JSON 数组, 形如 [{\"id\": \"...\", \"text\": \"译文\"}], 不要解释或代码块标记。"
)

# 润色: 不改原意, 只理顺语序、统一文风、去翻译腔 (整段为单位)
_POLISH_SYSTEM_PROMPT = (
    "你是资深文学编辑。下面是一段由小说翻译而来的译文。请在完全保留原意、人物称谓与情节的前提下, "
    "使其更自然流畅、符合目标语言阅读习惯, 消除翻译腔与生硬语序。"
    "不要增删情节, 不要加入原文没有的信息, 只输出润色后的正文, 不要任何解释。"
)


class TextTranslator:
    def __init__(self) -> None:
        self.cache = FileCache(settings.cache_dir)
        self.glossary = GlossaryStore(settings.glossary_dir)

    def _client(self):
        if OpenAI is None:
            raise RuntimeError("未安装 openai SDK, 请 pip install openai")
        if not settings.llm_ready:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY")
        return OpenAI(api_key=settings.deepseek_api_key, base_url=settings.base_url)

    def translate_batch(
        self,
        segments: list[Segment],
        target_lang: str,
        source_lang: str = "auto",
        series: str | None = None,
        context: list[str] | None = None,
        style: str = "general",
        polish: bool = False,
    ) -> tuple[list[TranslatedSegment], int]:
        """翻译一组文本段, 返回 (结果, 命中缓存数)。

        style="novel" 走文学直译; polish=True 额外对每段做二阶段润色。
        缓存键含 style+polish, 不同风格/润色互不污染。
        """
        terms = self.glossary.load(series) if series else []
        results: dict[str, TranslatedSegment] = {}
        pending: list[Segment] = []
        cached_hits = 0
        variant = f"{style}:{'p' if polish else 'np'}"

        # 1) 缓存命中
        for seg in segments:
            key = make_key("text", target_lang, series or "", variant, seg.text)
            hit = self.cache.get(key)
            if hit:
                cached_hits += 1
                results[seg.id] = TranslatedSegment(
                    id=seg.id, text=hit["text"], used_terms=hit.get("used_terms", [])
                )
            else:
                pending.append(seg)

        # 2) 未命中的分批送 LLM
        for i in range(0, len(pending), MAX_SEGMENTS_PER_BATCH):
            chunk = pending[i : i + MAX_SEGMENTS_PER_BATCH]
            translated = self._translate_chunk(
                chunk, target_lang, source_lang, terms, context, style, polish
            )
            for seg, tr in zip(chunk, translated):
                results[seg.id] = tr
                if tr.error is None:
                    key = make_key("text", target_lang, series or "", variant, seg.text)
                    self.cache.set(key, {"text": tr.text, "used_terms": tr.used_terms})

        ordered = [results[s.id] for s in segments]
        return ordered, cached_hits

    def polish_text(self, text: str, target_lang: str) -> str:
        """对整段译文做通顺润色 (二阶段)。失败则原样返回, 不中断管线。"""
        if not text.strip() or not settings.polish_enabled:
            return text
        user = f"目标语言: {target_lang}\n\n待润色译文:\n{text}"
        out = self._call_llm(_POLISH_SYSTEM_PROMPT, user, model=settings.polish_model)
        return out.strip() if out else text

    def _translate_chunk(
        self,
        chunk: list[Segment],
        target_lang: str,
        source_lang: str,
        terms,
        context,
        style: str = "general",
        polish: bool = False,
    ) -> list[TranslatedSegment]:
        hint = self.glossary.prompt_hint(terms)
        ctx = "\n".join(context[-3:]) if context else ""
        user_payload = {
            "target_lang": target_lang,
            "source_lang": source_lang,
            "context": ctx,
            "segments": [{"id": s.id, "text": s.text} for s in chunk],
        }
        base = _NOVEL_SYSTEM_PROMPT if style == "novel" else _SYSTEM_PROMPT
        sys_prompt = base + (f"\n\n{hint}" if hint else "")

        raw = self._call_llm(sys_prompt, json.dumps(user_payload, ensure_ascii=False))
        if raw is None:
            # 整批失败: 原文兜底
            return [
                TranslatedSegment(id=s.id, text=s.text, error="llm_failed")
                for s in chunk
            ]

        parsed = self._parse_aligned(raw, chunk)
        out: list[TranslatedSegment] = []
        for s in chunk:
            text = parsed.get(s.id, s.text)
            text, used = self.glossary.enforce(text, terms)
            if polish and s.id in parsed:
                text = self.polish_text(text, target_lang)
            out.append(
                TranslatedSegment(
                    id=s.id,
                    text=text,
                    used_terms=used,
                    error=None if s.id in parsed else "missing_segment",
                )
            )
        return out

    def _call_llm(
        self, system: str, user: str, retries: int = 4, model: str | None = None
    ) -> str | None:
        """调用 DeepSeek, 指数退避重试。全部失败返回 None。"""
        delay = 4.0
        for attempt in range(retries):
            try:
                client = self._client()
                resp = client.chat.completions.create(
                    model=model or settings.model,
                    temperature=0.2,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                return resp.choices[0].message.content
            except Exception as exc:  # noqa: BLE001 - 统一兜底重试
                logger.warning("LLM 调用失败 (%d/%d): %s", attempt + 1, retries, exc)
                if attempt < retries - 1:
                    time.sleep(delay)
                    delay *= 2
        return None

    @staticmethod
    def _parse_aligned(raw: str, chunk: list[Segment]) -> dict[str, str]:
        """解析 LLM 返回的对齐 JSON 数组, 容忍包裹的代码块。"""
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("[") : text.rfind("]") + 1]
        try:
            arr = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("LLM 返回非法 JSON, 该批回退原文")
            return {}
        out: dict[str, str] = {}
        for item in arr:
            if isinstance(item, dict) and "id" in item and "text" in item:
                out[str(item["id"])] = str(item["text"])
        return out
