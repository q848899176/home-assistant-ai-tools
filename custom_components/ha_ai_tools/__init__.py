"""Home Assistant AI Tools."""

from homeassistant.helpers import config_validation as cv

CONFIG_SCHEMA = cv.config_entry_only_config_schema("ha_ai_tools")


async def async_setup(hass, config):
    from pathlib import Path

    from homeassistant.components.http import StaticPathConfig

    from .api import async_register
    from .services import async_setup_services

    if not hass.data.get("ha_ai_tools_registered"):
        async_register(hass)
        await async_setup_services(hass)
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    "/ha_ai_tools/panel.js",
                    str(Path(__file__).parent / "frontend" / "panel.js"),
                    False,
                )
            ]
        )
        hass.data["ha_ai_tools_registered"] = True
    return True


async def async_setup_entry(hass, entry):
    from homeassistant.components import panel_custom

    from .manager import ToolsManager
    from .provider import async_import_legacy, list_connections

    manager = ToolsManager(hass, entry)
    await manager.async_load()
    entry.runtime_data = manager
    hass.data["ha_ai_tools"] = manager
    if (
        manager.settings.get("source_entity")
        and not entry.data.get("native_migration_completed")
        and not list_connections(entry)
    ):
        try:
            await async_import_legacy(manager, manager.settings["source_entity"])
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "native_migration_completed": True}
            )
        except Exception:
            manager.migration_error = (
                "原模型配置导入未完成，请到模型连接页面检查后重试。现有提醒继续运行。"
            )
    await hass.config_entries.async_forward_entry_setups(entry, ["conversation"])
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="ha-ai-tools-panel",
        frontend_url_path="ha-ai-tools",
        module_url="/ha_ai_tools/panel.js?v=0.2.0",
        sidebar_title="AI 工具",
        sidebar_icon="mdi:brain",
        require_admin=True,
    )
    manager.async_start()
    entry.async_on_unload(manager.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_update_entry))
    return True


async def _async_update_entry(hass, entry):
    # Keep one store owner and reminder scheduler while editing model subentries.
    async with entry.runtime_data.profile_lock:
        if await hass.config_entries.async_unload_platforms(entry, ["conversation"]):
            await hass.config_entries.async_forward_entry_setups(entry, ["conversation"])


async def async_unload_entry(hass, entry):
    from homeassistant.components import frontend

    from .provider import async_close_providers

    if await hass.config_entries.async_unload_platforms(entry, ["conversation"]):
        await entry.runtime_data.async_shutdown()
        await async_close_providers(entry.runtime_data)
        hass.data.pop("ha_ai_tools", None)
        frontend.async_remove_panel(hass, "ha-ai-tools")
        return True
    return False
