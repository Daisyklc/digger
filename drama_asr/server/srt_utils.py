"""Pure helpers for turning timed segments into SRT subtitles.

These functions have no heavy dependencies and are covered by unit tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Segment:
    """A single timed line of dialogue."""

    start: float  # seconds
    end: float  # seconds
    ja: str  # recognised Japanese text
    zh: str | None = None  # translated text (None if translation disabled)

    def to_dict(self) -> dict:
        return asdict(self)


def format_timestamp(seconds: float) -> str:
    """Format a time offset as an SRT timestamp ``HH:MM:SS,mmm``."""
    if seconds < 0:
        seconds = 0.0
    milliseconds = int(round(seconds * 1000.0))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def _render(segments: list[Segment], lines_for) -> str:
    blocks: list[str] = []
    index = 1
    for seg in segments:
        text_lines = [ln for ln in lines_for(seg) if ln and ln.strip()]
        if not text_lines:
            continue
        timing = f"{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}"
        body = "\n".join(text_lines)
        blocks.append(f"{index}\n{timing}\n{body}\n")
        index += 1
    # SRT blocks are separated by a blank line and the file ends with a newline.
    return "\n".join(blocks) + ("\n" if blocks else "")


def build_srt(segments: list[Segment], mode: str = "bilingual") -> str:
    """Render segments to SRT text.

    ``mode`` is one of ``"ja"`` (Japanese only), ``"zh"`` (translation only) or
    ``"bilingual"`` (Japanese on top, translation below).
    """
    if mode == "ja":
        return _render(segments, lambda s: [s.ja])
    if mode == "zh":
        return _render(segments, lambda s: [s.zh or ""])
    if mode == "bilingual":
        return _render(segments, lambda s: [s.ja, s.zh or ""])
    raise ValueError(f"unknown srt mode: {mode!r}")
