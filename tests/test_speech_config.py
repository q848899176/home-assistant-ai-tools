from types import SimpleNamespace

import pytest


def config(**changes):
    return {"id": "local", "name": "本地语音", "base_url": "http://speech.example.invalid:18973/v1",
            "api_key": "PRIVATE_SENTINEL", "capabilities": ["stt", "tts"],
            "stt_model": "asr", "tts_model": "tts", "voice": "Serena", **changes}


def test_empty_key_preserves_and_clear_is_explicit():
    from custom_components.ha_ai_tools.speech.config import validate_speech

    old = validate_speech(config())
    assert validate_speech({"api_key": ""}, old)["api_key"] == "PRIVATE_SENTINEL"
    with pytest.raises(ValueError):
        validate_speech({"clear_api_key": True}, old)


def test_capabilities_are_independent_and_page_state_is_safe():
    from custom_components.ha_ai_tools.speech.config import list_speech, validate_speech

    value = validate_speech(config(capabilities=["tts"], stt_model=""))
    entry = SimpleNamespace(subentries={"s": SimpleNamespace(subentry_type="speech", subentry_id="s", data=value)})
    safe = list_speech(entry)[0]
    assert safe["api_key_configured"] is True
    assert "PRIVATE_SENTINEL" not in repr(safe)
    assert value["capabilities"] == ["tts"]


@pytest.mark.parametrize("change", [{"base_url": "file:///private/test"}, {"voice": ""}, {"speed": 0}, {"capabilities": []}])
def test_invalid_configuration_is_rejected(change):
    from custom_components.ha_ai_tools.speech.config import validate_speech

    with pytest.raises(ValueError):
        validate_speech(config(**change))


async def test_probe_failure_keeps_old_subentry(monkeypatch):
    from custom_components.ha_ai_tools.speech.client import SpeechClient
    from custom_components.ha_ai_tools.speech.config import async_save_speech
    from tests.test_manager import manager

    m = manager()
    m.entry.subentries = {}

    async def fail(self):
        raise ValueError("服务不可用")

    monkeypatch.setattr(SpeechClient, "async_probe", fail)
    with pytest.raises(ValueError):
        await async_save_speech(m, config())
    assert m.entry.subentries == {}
