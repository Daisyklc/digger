"""Speech recognition backends.

The real backend wraps faster-whisper (CTranslate2) loaded with the
``anime-whisper`` model, which is fine-tuned on anime/galgame voice data and
transcribes emotional and sensitive (NSFW) speech that vanilla Whisper tends to
drop. Heavy libraries are imported lazily so this module can be imported in a
GPU-less / mock environment.
"""

from __future__ import annotations

from typing import Callable, Iterable, Protocol

RawSegment = tuple[float, float, str]
ProgressCb = Callable[[float], None]


class ASRBackend(Protocol):
    def transcribe(self, audio_path: str, progress: ProgressCb | None = None) -> list[RawSegment]:
        ...


class AnimeWhisperASR:
    """faster-whisper backend tuned for anime/drama audio."""

    def __init__(
        self,
        model_path: str,
        device: str = "cuda",
        compute_type: str = "float16",
        language: str = "ja",
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> None:
        from faster_whisper import WhisperModel  # lazy import

        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self._model = WhisperModel(model_path, device=device, compute_type=compute_type)

    def transcribe(self, audio_path: str, progress: ProgressCb | None = None) -> list[RawSegment]:
        segments_iter, info = self._model.transcribe(
            audio_path,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
            # Keep the original wording; drama scripts rely on punctuation/kana.
            condition_on_previous_text=True,
        )
        total = getattr(info, "duration", 0.0) or 0.0
        results: list[RawSegment] = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if text:
                results.append((float(seg.start), float(seg.end), text))
            if progress and total > 0:
                progress(min(0.99, float(seg.end) / total))
        if progress:
            progress(1.0)
        return results


class MockASR:
    """Deterministic stub used for tests and ``MOCK=1`` smoke runs.

    Produces a few fake Japanese lines so the end-to-end HTTP pipeline can be
    exercised without a GPU or any downloaded model.
    """

    _LINES = [
        "こんにちは、今日はいい天気ですね。",
        "……んっ、ちょっと待って。",
        "もう少しだけ、そばにいて。",
        "ありがとう、またね。",
    ]

    def transcribe(self, audio_path: str, progress: ProgressCb | None = None) -> list[RawSegment]:
        results: list[RawSegment] = []
        for i, line in enumerate(self._LINES):
            start = float(i * 3)
            end = start + 2.5
            results.append((start, end, line))
            if progress:
                progress((i + 1) / len(self._LINES))
        return results


def build_asr(settings) -> ASRBackend:
    if settings.mock:
        return MockASR()
    return AnimeWhisperASR(
        model_path=settings.asr_model,
        device=settings.device,
        compute_type=settings.compute_type,
        language=settings.asr_language,
        beam_size=settings.beam_size,
        vad_filter=settings.vad_filter,
    )
