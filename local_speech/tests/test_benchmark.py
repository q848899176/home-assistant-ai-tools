import io
import wave


def test_header_does_not_count_as_audio():
    from ha_ai_tools_speech.benchmark import audio_stats

    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
    assert audio_stats(out.getvalue())["valid_audio"] is False


def test_cold_warm_cache_and_queue_are_separate():
    from ha_ai_tools_speech.benchmark import summarize

    result = summarize([
        {"cold": True, "cached": False, "elapsed_ms": 10000},
        {"cold": False, "cached": True, "elapsed_ms": 1},
        {"cold": False, "cached": False, "elapsed_ms": 100},
        {"cold": False, "cached": False, "elapsed_ms": 200},
    ])
    assert result["warm_uncached"]["count"] == 2
    assert result["warm_uncached"]["p50_ms"] == 150
