"""Glue: audio file -> recognised + translated segments -> SRT outputs.

The backends are injected so this orchestration logic is fully testable with
mock ASR / translation implementations.
"""

from __future__ import annotations

from typing import Callable

from .asr import ASRBackend
from .srt_utils import Segment, build_srt
from .translate import Translator

ProgressCb = Callable[[str, float], None]


def transcribe_and_translate(
    audio_path: str,
    asr: ASRBackend,
    translator: Translator,
    progress: ProgressCb | None = None,
) -> dict:
    """Run the full pipeline and return a JSON-serialisable result dict."""

    def asr_progress(frac: float) -> None:
        if progress:
            # ASR occupies the first 80% of the reported progress.
            progress("asr", 0.8 * frac)

    raw = asr.transcribe(audio_path, progress=asr_progress)

    texts = [t for _, _, t in raw]
    translations = translator.translate_batch(texts)
    if len(translations) != len(texts):
        # Be defensive: never let a mismatched backend corrupt alignment.
        translations = (translations + [""] * len(texts))[: len(texts)]
    if progress:
        progress("translate", 0.95)

    segments = [
        Segment(start=start, end=end, ja=text, zh=(zh or None))
        for (start, end, text), zh in zip(raw, translations)
    ]

    has_translation = any(seg.zh for seg in segments)
    result = {
        "segments": [seg.to_dict() for seg in segments],
        "srt_ja": build_srt(segments, "ja"),
    }
    if has_translation:
        result["srt_zh"] = build_srt(segments, "zh")
        result["srt_bilingual"] = build_srt(segments, "bilingual")
    if progress:
        progress("done", 1.0)
    return result
