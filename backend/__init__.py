"""WebTranslator 后端服务包。

面向网页端电子书 / 在线漫画的翻译后端, 提供:
    - 文本型批量翻译 (上下文 + 术语一致)        -> text_translator
    - 漫画型流水线 (OCR -> 翻译 -> 擦字 -> 重排)  -> manga_pipeline
    - 术语库 (跨章节专有名词一致)                -> glossary
    - 译文缓存                                    -> cache

对外统一通过 FastAPI 暴露 (见 server.py)。
"""

__all__ = ["config"]
