"""术语库: 保证长篇连载的专有名词跨章节一致。

按作品 series 分文件存储 (data/glossary/{series}.json)。
核心能力:
    - load/save        读写术语
    - lock             锁定某术语译法 (锁定后翻译不再变动)
    - prompt_hint      生成注入 LLM 的术语约束文本
    - enforce          对译文做术语强制替换 (兜底一致性)
"""

from __future__ import annotations

import json
import os
import re

from .schemas import GlossaryTerm


class GlossaryStore:
    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    def _path(self, series: str) -> str:
        safe = re.sub(r"[^\w\-]", "_", series)
        return os.path.join(self.root, f"{safe}.json")

    def load(self, series: str) -> list[GlossaryTerm]:
        path = self._path(series)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [GlossaryTerm(**t) for t in data.get("terms", [])]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def save(self, series: str, terms: list[GlossaryTerm], target_lang: str) -> None:
        path = self._path(series)
        payload = {
            "series": series,
            "target_lang": target_lang,
            "terms": [t.model_dump() for t in terms],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def prompt_hint(self, terms: list[GlossaryTerm]) -> str:
        """把术语转成 LLM system 约束文本。仅包含已锁定或已确认术语。"""
        active = [t for t in terms if t.dst]
        if not active:
            return ""
        lines = [f"- {t.src} => {t.dst}" for t in active]
        return "以下专有名词必须严格采用给定译法, 不得改动:\n" + "\n".join(lines)

    def enforce(self, text: str, terms: list[GlossaryTerm]) -> tuple[str, list[str]]:
        """对译文做术语强制替换, 返回 (结果文本, 命中术语列表)。

        兜底策略: 即便 LLM 未遵守, 也保证最终译文术语一致。
        """
        used: list[str] = []
        for t in terms:
            if not t.src or not t.dst:
                continue
            if t.src in text:
                text = text.replace(t.src, t.dst)
                used.append(t.dst)
        return text, used
