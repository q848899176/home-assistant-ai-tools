import pytest

from custom_components.ha_ai_tools.api import validate_settings
from tests.test_manager import manager


def test_settings_refuses_non_weather_entities_for_location():
    with pytest.raises(ValueError):
        validate_settings(manager(), {"locations": {"家": "text.speaker_play_text"}})


def test_settings_refuses_incapable_output():
    with pytest.raises(ValueError):
        validate_settings(manager(), {"outputs": ["weather.home"]})


def test_settings_preserves_selected_outputs_and_weather_map():
    value = validate_settings(
        manager(), {"outputs": ["text.speaker_play_text"], "locations": {"家": "weather.home"}}
    )
    assert value["outputs"] == ["text.speaker_play_text"]
    assert value["locations"] == {"家": "weather.home"}


async def test_management_api_updates_the_selected_reminder_devices():
    from custom_components.ha_ai_tools.api import async_action

    m = manager()
    await m.async_load()
    r = await async_action(m, "add_reminder", {"message": "喝水", "in_minutes": 5})
    result = await async_action(
        m, "update_reminder_outputs", {"id": r["id"], "outputs": ["assist_satellite.voice"]}
    )
    assert result["id"] == r["id"]
    assert m.store.data["reminders"][0]["outputs"] == ["assist_satellite.voice"]


async def test_management_api_deletes_history_and_preserves_active_reminder():
    from custom_components.ha_ai_tools.api import async_action

    m = manager()
    await m.async_load()
    history = await m.async_add_reminder({"message": "历史记录", "in_minutes": 60}, "history")
    await m.async_cancel_reminder(history["id"])
    pending = await m.async_add_reminder({"message": "洗头", "in_minutes": 120}, "pending")
    result = await async_action(m, "delete_reminder_history", {"id": history["id"]})
    assert result["deleted"] == 1
    assert m.data["reminders"] == [pending]
    assert await async_action(m, "clear_reminder_history", {}) == {"deleted": 0}


async def test_home_assistant_voice_is_also_known_as_nabu(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry, device_registry, entity_registry

    from custom_components.ha_ai_tools.api import broadcast_devices

    hass = HomeAssistant(str(tmp_path))
    hass.states.async_set(
        "assist_satellite.home_assistant_voice_test",
        "idle",
        {"friendly_name": "Home Assistant Voice", "supported_features": 1},
    )
    registry = SimpleNamespace(async_get=lambda key: None, async_get_area=lambda key: None)
    for module in (area_registry, device_registry):
        monkeypatch.setattr(module, "async_get", lambda hass: registry)
    entity = SimpleNamespace(device_id=None, area_id=None, aliases={"Nabu"})
    monkeypatch.setattr(
        entity_registry, "async_get", lambda hass: SimpleNamespace(async_get=lambda key: entity)
    )
    m = manager()
    m.hass = hass
    assert broadcast_devices(m)[0]["aliases"] == ["Nabu"]


async def test_snapshot_never_returns_legacy_or_native_connection_credentials(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry, device_registry, entity_registry

    from custom_components.ha_ai_tools.api import snapshot

    m = manager()
    m.entry.data = {"api_key": "PRIVATE_SENTINEL", "outputs": [], "locations": {}}
    m.entry.subentries = {
        "connection": SimpleNamespace(
            subentry_type="connection",
            subentry_id="connection",
            data={
                "id": "first",
                "name": "连接",
                "api_key": "PRIVATE_SENTINEL",
                "base_url": "https://api.example.invalid/v1",
                "api_provider": "openai",
            },
        )
    }
    m.hass = HomeAssistant(str(tmp_path))
    empty = SimpleNamespace(
        async_get=lambda key: None, async_get_area=lambda key: None, entities={}
    )
    for module in (area_registry, device_registry, entity_registry):
        monkeypatch.setattr(module, "async_get", lambda hass: empty)
    value = snapshot(m)
    assert "PRIVATE_SENTINEL" not in str(value)
    assert "api_key" not in value["settings"]
    assert "connections" in value and "profiles" in value


async def test_legacy_preview_excludes_credentials_and_reports_parameters(monkeypatch):
    from types import SimpleNamespace

    from custom_components.ha_ai_tools import provider
    from custom_components.ha_ai_tools.api import async_action

    m = manager()
    m.hass.data = {}
    legacy = SimpleNamespace(
        title="旧连接",
        options={"chat_model": "old-model", "functions": "[]"},
        subentries={},
        data={"api_key": "PRIVATE_SENTINEL"},
    )
    monkeypatch.setattr(provider, "get_provider", lambda *_: (legacy, None))
    result = await async_action(m, "preview_legacy", {"source_entity": "conversation.old"})
    assert "PRIVATE_SENTINEL" not in str(result)
    assert result["profiles"][0]["chat_model"] == "old-model"
    assert result["profiles"][0]["function_count"] == 0


def test_advanced_function_authentication_is_hidden_from_page_state():
    from custom_components.ha_ai_tools.api import _function_credentials

    assert _function_credentials(
        [{"function": {"type": "rest", "headers": {"Authorization": "credential"}}}]
    )
    assert _function_credentials("- function:\n    type: rest\n    password: credential\n")
    assert not _function_credentials(
        '- function:\n    type: template\n    value_template: "{{ value }}"\n'
    )
