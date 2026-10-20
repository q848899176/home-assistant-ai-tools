"""用当前真实 HA 框架验证关键接口，避免依赖假对象遗漏兼容问题。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from homeassistant.components.config.automation import EditAutomationConfigView
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv

from custom_components.ha_ai_tools.tools import native_automation_view


async def test_finds_automation_view_registered_by_real_homeassistant(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    app = web.Application()

    async def validate(hass, key, config):
        return config

    view = EditAutomationConfigView(
        "automation", "config", "automations.yaml", cv.string, data_validator=validate
    )
    view.register(hass, app, app.router)
    hass.http = SimpleNamespace(app=app)
    assert native_automation_view(hass) is view


async def test_options_without_weather_can_be_saved(monkeypatch):
    from custom_components.ha_ai_tools import config_flow
    from tests.test_manager import manager

    m = manager()
    m.entry.options["locations"] = {}
    await m.async_load()
    flow = config_flow.AIToolsOptionsFlow()
    monkeypatch.setattr(
        config_flow.AIToolsOptionsFlow,
        "config_entry",
        property(lambda self: SimpleNamespace(runtime_data=m)),
    )
    flow.hass = m.hass
    monkeypatch.setattr(config_flow, "get_provider", lambda hass, source: (object(), "test"))
    result = await flow.async_step_init({"source_entity": "conversation.test", "outputs": []})
    assert result["type"] == "create_entry"
    assert m.settings["locations"] == {}


@pytest.mark.parametrize("command", ["ha_ai_tools/data", "ha_ai_tools/action"])
async def test_admin_commands_use_real_websocket_authorization(tmp_path, command):
    from custom_components.ha_ai_tools.api import async_register

    hass = HomeAssistant(str(tmp_path))
    async_register(hass)
    done, sent = asyncio.Event(), []

    def receive(message):
        sent.append(json.loads(message) if isinstance(message, (bytes, str)) else message)
        done.set()

    user = SimpleNamespace(is_admin=True, name="Test admin")
    connection = ActiveConnection(Mock(), hass, receive, user, None, None)
    handler, _ = hass.data["websocket_api"][command]
    message = {"id": 1, "type": command, "action": "save_settings"}
    handler(hass, connection, message)
    await asyncio.wait_for(done.wait(), 1)
    assert sent[0]["error"]["code"] == "not_ready"
    user.is_admin = False
    with pytest.raises(Unauthorized):
        handler(hass, connection, message)


async def test_native_automation_write_and_failed_reload_reuse_one_id(tmp_path, monkeypatch):
    import yaml

    from custom_components.ha_ai_tools import tools
    from custom_components.ha_ai_tools.manager import ToolsManager
    from tests.test_manager import Store

    hass = HomeAssistant(str(tmp_path))
    app = web.Application()

    async def validate(hass, key, config):
        return config

    view = EditAutomationConfigView(
        "automation", "config", "automations.yaml", cv.string, data_validator=validate
    )
    view.register(hass, app, app.router)
    hass.http = SimpleNamespace(app=app)
    (tmp_path / "automations.yaml").write_text("[]\n")

    async def actor(hass, user_input):
        return None

    async def fail_reload(call):
        raise OSError("synthetic reload failure")

    hass.services.async_register("automation", "reload", fail_reload)
    monkeypatch.setattr(tools, "require_automation_actor", actor)
    monkeypatch.setattr(tools, "exposed_entities", lambda hass: {})
    entry = SimpleNamespace(entry_id="test", data={}, options={})
    manager = ToolsManager(hass, entry, Store())
    await manager.async_load()
    from homeassistant.core import Context

    user_input = SimpleNamespace(context=Context())
    config = "alias: 测试\ntriggers: []\nactions: []\n"
    first = await tools.create_automation(manager, config, user_input, "turn1")
    second = await tools.create_automation(manager, config, user_input, "turn2")
    assert first["saved"] and not first["loaded"]
    assert first["id"] == second["id"]
    saved = yaml.safe_load((tmp_path / "automations.yaml").read_text())
    assert len(saved) == 1 and saved[0]["id"] == first["id"]


async def test_speech_apply_restore_uses_real_ha_pipeline_collection(tmp_path, monkeypatch):
    from homeassistant.components.assist_pipeline.models import Pipeline
    from homeassistant.components.assist_pipeline.pipeline import (
        KEY_ASSIST_PIPELINE,
        PipelineStorageCollection,
        PipelineStore,
    )

    from custom_components.ha_ai_tools.speech.pipeline import PipelineManager, SpeechStore
    from tests.test_manager import Store

    hass = HomeAssistant(str(tmp_path))
    collection = PipelineStorageCollection(PipelineStore(hass, 1, "test.pipeline"))
    original = Pipeline(conversation_engine="conversation.home_assistant", conversation_language="zh-CN",
                        language="zh", name="原助手", stt_engine="stt.old", stt_language="zh-CN",
                        tts_engine="tts.old", tts_language="zh_CN", tts_voice="original",
                        wake_word_entity=None, wake_word_id=None, prefer_local_intents=True)
    collection.data[original.id] = original
    collection._preferred_item = original.id
    monkeypatch.setattr(collection, "_async_schedule_save", lambda: None)
    hass.data[KEY_ASSIST_PIPELINE] = SimpleNamespace(pipeline_store=collection)
    store = SpeechStore(hass, "test", store=Store())
    await store.async_load()
    manager = PipelineManager(hass, store)
    point = await manager.async_preview({"pipelines": [{"id": original.id, "fields": {
        "stt_engine": "stt.local", "stt_language": "zh", "tts_engine": "tts.local", "tts_language": "zh", "tts_voice": "Serena"}}]})
    assert (await manager.async_apply(point["id"], point["fingerprint"]))["applied"]
    updated = collection.data[original.id]
    assert updated.tts_voice == "Serena" and updated.prefer_local_intents
    assert updated.conversation_engine == original.conversation_engine
    assert collection.async_get_preferred_item() == original.id
    assert (await manager.async_restore(point["id"]))["status"] == "restored"
    assert collection.data[original.id] == original
