import asyncio
from types import SimpleNamespace

from custom_components.ha_ai_tools import _async_update_entry, async_setup_entry


async def test_migration_failure_keeps_existing_reminder_manager_running(monkeypatch):
    from homeassistant.components import panel_custom

    from custom_components.ha_ai_tools import manager as module
    from custom_components.ha_ai_tools import provider

    calls = []

    async def noop(*args, **kwargs):
        return None

    async def fail(*args):
        raise ValueError("offline")

    entry = SimpleNamespace(
        data={}, async_on_unload=lambda cb: None, add_update_listener=lambda cb: lambda: None
    )
    instance = SimpleNamespace(
        settings={"source_entity": "conversation.old"},
        async_load=noop,
        async_start=lambda: calls.append("started"),
        async_stop=lambda: None,
    )
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(async_forward_entry_setups=noop))
    monkeypatch.setattr(module, "ToolsManager", lambda *_: instance)
    monkeypatch.setattr(provider, "list_connections", lambda _: [])
    monkeypatch.setattr(provider, "async_import_legacy", fail)
    monkeypatch.setattr(panel_custom, "async_register_panel", noop)
    assert await async_setup_entry(hass, entry)
    assert calls == ["started"]
    assert instance.migration_error and hass.data["ha_ai_tools"] is instance


async def test_profile_refresh_keeps_one_manager_and_serializes_unload_forward():
    calls = []

    async def unload(*args):
        calls.append("unload")
        await asyncio.sleep(0)
        return True

    async def forward(*args):
        calls.append("forward")

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=unload, async_forward_entry_setups=forward
        )
    )
    manager = SimpleNamespace(profile_lock=asyncio.Lock())
    entry = SimpleNamespace(runtime_data=manager)
    await asyncio.gather(_async_update_entry(hass, entry), _async_update_entry(hass, entry))
    assert calls == ["unload", "forward", "unload", "forward"]
    assert entry.runtime_data is manager
