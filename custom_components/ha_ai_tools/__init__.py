"""Home Assistant AI Tools."""


async def async_setup(hass, config):
    from pathlib import Path

    from homeassistant.components.http import StaticPathConfig

    from .api import async_register

    if not hass.data.get("ha_ai_tools_registered"):
        async_register(hass)
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

    manager = ToolsManager(hass, entry)
    await manager.async_load()
    entry.runtime_data = manager
    hass.data["ha_ai_tools"] = manager
    await hass.config_entries.async_forward_entry_setups(entry, ["conversation"])
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="ha-ai-tools-panel",
        frontend_url_path="ha-ai-tools",
        module_url="/ha_ai_tools/panel.js?v=0.1.2",
        sidebar_title="AI 工具",
        sidebar_icon="mdi:brain",
        require_admin=True,
    )
    manager.async_start()
    entry.async_on_unload(manager.async_stop)
    return True


async def async_unload_entry(hass, entry):
    from homeassistant.components import frontend

    if await hass.config_entries.async_unload_platforms(entry, ["conversation"]):
        entry.runtime_data.async_stop()
        hass.data.pop("ha_ai_tools", None)
        frontend.async_remove_panel(hass, "ha-ai-tools")
        return True
    return False
