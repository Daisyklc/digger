"""WebTranslator 后端配置。

配置来源优先级: 请求参数 > 环境变量 > 常量默认值。
与现有 TaskMiner 模块保持一致: LLM 使用 DeepSeek (兼容 OpenAI SDK)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TARGET_LANG = "zh-CN"
DEFAULT_SOURCE_LANG = "auto"

# 单次请求最多打包的文本段数, 避免超 token 与漏段
MAX_SEGMENTS_PER_BATCH = 40

# 引擎版本: 参与缓存键, 升级提示词/模型时自动绕过旧缓存
ENGINE_VERSION = "1"


@dataclass
class Settings:
    """运行期配置, 由环境变量装配。"""

    deepseek_api_key: str | None = field(
        default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY")
    )
    model: str = field(
        default_factory=lambda: os.environ.get("WT_LLM_MODEL", DEFAULT_MODEL)
    )
    base_url: str = DEEPSEEK_BASE_URL
    target_lang: str = field(
        default_factory=lambda: os.environ.get("WT_TARGET_LANG", DEFAULT_TARGET_LANG)
    )
    ocr_backend: str = field(
        default_factory=lambda: os.environ.get("WT_OCR_BACKEND", "manga-ocr")
    )
    glossary_dir: str = field(
        default_factory=lambda: os.environ.get("WT_GLOSSARY_DIR", "./data/glossary")
    )
    cache_dir: str = field(
        default_factory=lambda: os.environ.get("WT_CACHE_DIR", "./data/cache")
    )

    @property
    def llm_ready(self) -> bool:
        """LLM 是否已就绪 (是否配置了 Key)。"""
        return bool(self.deepseek_api_key)

    def missing(self) -> list[str]:
        """返回缺失的关键配置项, 供 /healthz 暴露。"""
        gaps: list[str] = []
        if not self.deepseek_api_key:
            gaps.append("DEEPSEEK_API_KEY")
        return gaps


settings = Settings()
