"""译文缓存: 本地文件 KV。

键参与 engine_version, 升级提示词/模型时旧缓存自动失效。
接口预留以便切换 Redis。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from .config import ENGINE_VERSION


def make_key(*parts: str) -> str:
    """由任意字符串片段构造稳定缓存键 (含引擎版本)。"""
    raw = "\x1f".join([ENGINE_VERSION, *parts])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class FileCache:
    """极简文件 KV。生产可替换为 Redis, 保持相同 get/set 接口。"""

    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, key[:2], key + ".json")

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            # 损坏即视为未命中
            return None

    def set(self, key: str, value: Any) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False)
        except OSError:
            pass
