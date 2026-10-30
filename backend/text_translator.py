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
    ) -> tuple[list[TranslatedSegment], int]:
        """翻译一组文本段, 返回 (结果, 命中缓存数)。"""
        terms = self.glossary.load(series) if series else []
        results: dict[str, TranslatedSegment] = {}
        pending: list[Segment] = []
        cached_hits = 0

        # 1) 缓存命中
        for seg in segments:
            key = make_key("text", target_lang, series or "", seg.text)
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
                chunk, target_lang, source_lang, terms, context
            )
            for seg, tr in zip(chunk, translated):
                results[seg.id] = tr
                if tr.error is None:
                    key = make_key("text", target_lang, series or "", seg.text)
                    self.cache.set(key, {"text": tr.text, "used_terms": tr.used_terms})

        ordered = [results[s.id] for s in segments]
        return ordered, cached_hits

    def _translate_chunk(
        self,
        chunk: list[Segment],
        target_lang: str,
        source_lang: str,
        terms,
        context,
    ) -> list[TranslatedSegment]:
        hint = self.glossary.prompt_hint(terms)
        ctx = "\n".join(context[-3:]) if context else ""
        user_payload = {
            "target_lang": target_lang,
            "source_lang": source_lang,
            "context": ctx,
            "segments": [{"id": s.id, "text": s.text} for s in chunk],
        }
        sys_prompt = _SYSTEM_PROMPT + (f"\n\n{hint}" if hint else "")

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
            out.append(
                TranslatedSegment(
                    id=s.id,
                    text=text,
                    used_terms=used,
                    error=None if s.id in parsed else "missing_segment",
                )
            )
        return out

    def _call_llm(self, system: str, user: str, retries: int = 4) -> str | None:
        """调用 DeepSeek, 指数退避重试。全部失败返回 None。"""
        delay = 4.0
        for attempt in range(retries):
            try:
                client = self._client()
                resp = client.chat.completions.create(
                    model=settings.model,
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
