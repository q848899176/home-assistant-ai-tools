import asyncio
from copy import deepcopy
from types import SimpleNamespace

from tests.test_speech_config import config


def subentry(data):
    return SimpleNamespace(subentry_type="speech", subentry_id="s", data=data)


async def test_speech_update_keeps_manager_scheduler_and_sessions():
    from custom_components.ha_ai_tools import _async_update_entry
    from custom_components.ha_ai_tools.speech import SpeechRuntime

    writes = []

    async def unload(*args):
        writes.append("unload")
        return True

    async def forward(*args):
        writes.append("forward")

    entry = SimpleNamespace(entry_id="root", data={}, options={}, subentries={"s": subentry(config())})
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_unload_platforms=unload, async_forward_entry_setups=forward))
    manager = SimpleNamespace(profile_lock=asyncio.Lock(), data={"reminders": ["existing"], "sessions": {"s": "keep"}})
    entry.runtime_data = manager
    manager.speech = SpeechRuntime(hass, entry, manager)
    await manager.speech.async_load()
    before = deepcopy(manager.data)
    entry.subentries["s"].data = config(voice="Vivian")
    await _async_update_entry(hass, entry)
    assert writes == []
    assert entry.runtime_data is manager and manager.data == before
    assert manager.speech.configs["local"]["voice"] == "Vivian"


async def test_only_tts_config_loads_without_stt_entity():
    from custom_components.ha_ai_tools import stt, tts
    from custom_components.ha_ai_tools.speech import SpeechRuntime

    entry = SimpleNamespace(entry_id="root", subentries={"s": subentry(config(capabilities=["tts"]))})
    rt = SpeechRuntime(None, entry, None)
    await rt.async_load()
    entry.runtime_data = SimpleNamespace(speech=rt)
    found = []
    def add(entities, **kwargs):
        found.extend(entities)
    await stt.async_setup_entry(None, entry, add)
    await tts.async_setup_entry(None, entry, add)
    assert len(found) == 1
    assert isinstance(found[0], tts.AIToolsTTSEntity)


async def test_unavailable_speech_does_not_block_conversation():
    from custom_components.ha_ai_tools.speech import SpeechRuntime

    entry = SimpleNamespace(subentries={"s": subentry(config())})
    rt = SpeechRuntime(None, entry, None)
    await rt.async_load()
    assert rt.configs["local"]["name"] == "本地语音"
    assert rt.clients == {}
    await rt.async_close()


async def test_rename_and_reload_do_not_duplicate_entities():
    from custom_components.ha_ai_tools.speech import SpeechRuntime, async_refresh

    entry = SimpleNamespace(entry_id="root", subentries={"s": subentry(config())})
    manager = SimpleNamespace()
    manager.speech = SpeechRuntime(None, entry, manager)
    rt = manager.speech
    await rt.async_load()
    found = []
    rt.adders["tts"] = lambda entities, **kwargs: found.extend(entities)
    entry.subentries["s"].data = config(name="新的名称")
    await async_refresh(manager, {"local"})
    assert len(found) == 1
    assert found[0].unique_id == "root:speech:local:tts"


async def test_failed_model_platform_reload_retries_next_update():
    from custom_components.ha_ai_tools import _async_update_entry
    from custom_components.ha_ai_tools.speech import SpeechRuntime

    calls = []
    async def unload(*args):
        calls.append("unload")
        return len(calls) > 1
    async def forward(*args):
        calls.append("forward")
    entry = SimpleNamespace(entry_id="root", data={}, options={}, subentries={})
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_unload_platforms=unload, async_forward_entry_setups=forward))
    manager = SimpleNamespace(profile_lock=asyncio.Lock())
    manager.speech = SpeechRuntime(hass, entry, manager)
    await manager.speech.async_load()
    entry.runtime_data = manager
    entry.options = {"changed": True}
    await _async_update_entry(hass, entry)
    await _async_update_entry(hass, entry)
    assert calls == ["unload", "unload", "forward"]
