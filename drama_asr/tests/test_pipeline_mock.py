from drama_asr.server.asr import MockASR
from drama_asr.server.pipeline import transcribe_and_translate
from drama_asr.server.translate import MockTranslator, NoopTranslator


def test_pipeline_with_translation():
    stages = []
    result = transcribe_and_translate(
        "dummy.wav",
        MockASR(),
        MockTranslator(),
        progress=lambda stage, frac: stages.append((stage, frac)),
    )
    assert len(result["segments"]) == len(MockASR._LINES)
    first = result["segments"][0]
    assert first["ja"] == MockASR._LINES[0]
    assert first["zh"].startswith("[译]")
    assert "srt_ja" in result
    assert "srt_zh" in result
    assert "srt_bilingual" in result
    # Progress must reach completion.
    assert stages[-1] == ("done", 1.0)


def test_pipeline_without_translation():
    result = transcribe_and_translate("dummy.wav", MockASR(), NoopTranslator())
    assert "srt_ja" in result
    assert "srt_zh" not in result
    assert "srt_bilingual" not in result
    assert all(seg["zh"] is None for seg in result["segments"])


def test_pipeline_alignment_preserved():
    result = transcribe_and_translate("dummy.wav", MockASR(), MockTranslator())
    for seg in result["segments"]:
        assert seg["zh"] == f"[译] {seg['ja']}"
        assert seg["end"] > seg["start"]
