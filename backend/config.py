"""WebTranslator 后端配置。

配置来源优先级 (高 → 低):
    请求参数 > 环境变量 > config.local.yaml > 常量默认值。

本地密钥请写入仓库根目录的 config.local.yaml
(从 config.local.example.yaml 复制), 该文件已被 .gitignore。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TARGET_LANG = "zh-CN"
DEFAULT_SOURCE_LANG = "auto"

# 单次请求最多打包的文本段数, 避免超 token 与漏段
MAX_SEGMENTS_PER_BATCH = 40

# 引擎版本: 参与缓存键, 升级提示词/模型时自动绕过旧缓存
ENGINE_VERSION = "1"

# 跨页上下文: 保留上一页译文末尾多少字, 维持人称/语气跨页连贯
NOVEL_CONTEXT_CHARS = 500

# 仓库根目录 (backend/ 的上一级)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_CONFIG_PATH = _REPO_ROOT / "config.local.yaml"


def _parse_flat_yaml(path: Path) -> dict[str, str]:
    """极简扁平 YAML 读取 (只支持 key: value 单行, # 注释)。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


_LOCAL = _parse_flat_yaml(_LOCAL_CONFIG_PATH)


def _cfg(env_name: str, local_key: str, default: str | None = None) -> str | None:
    """环境变量优先, 其次 config.local.yaml, 再默认值。"""
    if env_name in os.environ and os.environ[env_name] != "":
        return os.environ[env_name]
    if local_key in _LOCAL and _LOCAL[local_key] != "":
        return _LOCAL[local_key]
    return default


@dataclass
class Settings:
    """运行期配置。"""

    deepseek_api_key: str | None = field(
        default_factory=lambda: _cfg("DEEPSEEK_API_KEY", "deepseek_api_key")
    )
    model: str = field(
        default_factory=lambda: _cfg("WT_LLM_MODEL", "model", DEFAULT_MODEL) or DEFAULT_MODEL
    )
    base_url: str = DEEPSEEK_BASE_URL
    target_lang: str = field(
        default_factory=lambda: _cfg("WT_TARGET_LANG", "target_lang", DEFAULT_TARGET_LANG)
        or DEFAULT_TARGET_LANG
    )
    ocr_backend: str = field(
        default_factory=lambda: _cfg("WT_OCR_BACKEND", "ocr_backend", "manga-ocr")
        or "manga-ocr"
    )
    glossary_dir: str = field(
        default_factory=lambda: _cfg("WT_GLOSSARY_DIR", "glossary_dir", "./data/glossary")
        or "./data/glossary"
    )
    cache_dir: str = field(
        default_factory=lambda: _cfg("WT_CACHE_DIR", "cache_dir", "./data/cache")
        or "./data/cache"
    )
    novel_dir: str = field(
        default_factory=lambda: _cfg("WT_NOVEL_DIR", "novel_dir", "./data/novel")
        or "./data/novel"
    )
    browser_profile: str = field(
        default_factory=lambda: _cfg("WT_BROWSER_PROFILE", "browser_profile", "./data/browser")
        or "./data/browser"
    )
    # 润色: 小说场景默认开启; 可指向更强模型 / 外部 API
    polish_enabled: bool = field(
        default_factory=lambda: (
            (_cfg("WT_POLISH", "polish", "true") or "true").lower() != "false"
        )
    )
    polish_model: str = field(
        default_factory=lambda: _cfg("WT_POLISH_MODEL", "polish_model")
        or _cfg("WT_LLM_MODEL", "model", DEFAULT_MODEL)
        or DEFAULT_MODEL
    )

    @property
    def llm_ready(self) -> bool:
        """LLM 是否已就绪 (是否配置了 Key)。"""
        key = self.deepseek_api_key or ""
        # 排除示例占位符
        if not key or "请替换" in key or key.startswith("sk-请"):
            return False
        return True

    def missing(self) -> list[str]:
        """返回缺失的关键配置项, 供 /healthz 暴露。"""
        gaps: list[str] = []
        if not self.llm_ready:
            gaps.append("deepseek_api_key (写在 config.local.yaml 或环境变量 DEEPSEEK_API_KEY)")
        return gaps


settings = Settings()
