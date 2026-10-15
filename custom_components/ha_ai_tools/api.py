"""管理员管理接口。"""

import logging
from copy import deepcopy

from . import models

DOMAIN = "ha_ai_tools"
_LOGGER = logging.getLogger(__name__)


def validate_settings(manager, value):
    if not isinstance(value, dict):
        raise ValueError("设置必须是对象")
    result = {}
    if "outputs" in value:
        outputs = value["outputs"]
        if not isinstance(outputs, list):
            raise ValueError("播报设备必须是列表")
        result["outputs"] = (
            manager.validate_outputs(outputs, selected_only=False) if outputs else []
        )
    if "locations" in value:
        locations = value["locations"]
        if not isinstance(locations, dict) or len(locations) > 20:
            raise ValueError("最多设置20个天气地点")
        clean = {}
        for name, entity in locations.items():
            name = models.bounded_text(name, "地点名称", 80)
            if (
                not isinstance(entity, str)
                or not entity.startswith("weather.")
                or manager.hass.states.get(entity) is None
            ):
                raise ValueError("地点必须选择已配置的天气实体")
            clean[name] = entity
        result["locations"] = clean
    if "source_entity" in value:
        result["source_entity"] = models.bounded_text(value["source_entity"], "模型来源", 150)
    return result


def broadcast_devices(manager):
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    hass = manager.hass
    entities, devices, areas = er.async_get(hass), dr.async_get(hass), ar.async_get(hass)
    outputs = []
    for state in hass.states.async_all():
        entity = entities.async_get(state.entity_id)
        if state.entity_id.startswith(("text.", "assist_satellite.")):
            try:
                manager.validate_outputs([state.entity_id], selected_only=False)
            except ValueError:
                continue
            device = devices.async_get(entity.device_id) if entity and entity.device_id else None
            area_id = (
                entity.area_id if entity and entity.area_id else device.area_id if device else None
            )
            area = areas.async_get_area(area_id) if area_id else None
            outputs.append(
                {
                    "entity_id": state.entity_id,
                    "name": state.name,
                    "aliases": sorted(entity.aliases) if entity else [],
                    "area": area.name if area else "未分配房间",
                    "available": state.state not in ("unavailable", "unknown"),
                }
            )
    return outputs


def snapshot(manager):
    from homeassistant.helpers import entity_registry as er

    hass = manager.hass
    entities = er.async_get(hass)
    weather, sources = [], []
    for state in hass.states.async_all():
        entity = entities.async_get(state.entity_id)
        if (
            entity
            and entity.platform == "extended_openai_conversation"
            and state.entity_id.startswith("conversation.")
        ):
            sources.append({"entity_id": state.entity_id, "name": state.name})
        if state.entity_id.startswith("weather."):
            weather.append({"entity_id": state.entity_id, "name": state.name, "state": state.state})
    return {
        "settings": dict(manager.settings),
        "outputs": broadcast_devices(manager),
        "weather": weather,
        "sources": sources,
        "reminders": deepcopy(manager.data["reminders"][-200:][::-1]),
        "history_count": sum(
            r["status"] in models.REMINDER_HISTORY_STATES for r in manager.data["reminders"]
        ),
        "memories": deepcopy(manager.data["memories"][::-1]),
        "session_count": len(manager.data["sessions"]),
        "timezone": "Asia/Shanghai",
    }


async def async_action(manager, action, payload):
    if action == "save_settings":
        settings = validate_settings(manager, payload)
        if "source_entity" in settings:
            from .provider import get_provider

            get_provider(manager.hass, settings["source_entity"])
        await manager.async_settings(settings)
        return {"saved": True}
    if action == "add_reminder":
        from uuid import uuid4

        return await manager.async_add_reminder(payload, uuid4().hex)
    if action == "cancel_reminder":
        return await manager.async_cancel_reminder(payload.get("id"))
    if action == "update_reminder_outputs":
        return await manager.async_update_reminder_outputs(payload.get("id"), payload.get("outputs"))
    if action == "delete_reminder_history":
        return await manager.async_delete_reminder_history(payload.get("id"))
    if action == "clear_reminder_history":
        return await manager.async_clear_reminder_history()
    if action == "remember":
        key = models.bounded_text(payload.get("key"), "记忆类别", 80)
        text = models.bounded_text(payload.get("text"), "记忆内容", 800)
        scope = payload.get("scope", "family")
        if not isinstance(scope, str) or not (scope == "family" or scope.startswith("user:")):
            raise ValueError("记忆归属不正确")
        return await manager.async_remember(
            {"key": key, "text": text, "evidence": "记住：" + text}, "记住：" + text, scope
        )
    if action == "forget":
        return await manager.async_forget(payload.get("id"))
    if action == "weather":
        return await manager.async_weather(payload.get("location"), payload.get("kind", "current"))
    if action == "test_broadcast":
        outputs = manager.validate_outputs(manager.settings.get("outputs", []))
        return await manager.async_broadcast("这是 AI 工具的播报测试。", outputs)
    raise ValueError("未知管理操作")


def async_register(hass):
    import probatio as vol
    from homeassistant.components import websocket_api

    @websocket_api.websocket_command({vol.Required("type"): "ha_ai_tools/data"})
    @websocket_api.require_admin
    @websocket_api.async_response
    async def data_command(hass, connection, msg):
        manager = hass.data.get(DOMAIN)
        if manager is None:
            connection.send_error(msg["id"], "not_ready", "请先添加 AI Tools 集成")
            return
        connection.send_result(msg["id"], snapshot(manager))

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "ha_ai_tools/action",
            vol.Required("action"): str,
            vol.Optional("payload", default=dict): dict,
        }
    )
    @websocket_api.require_admin
    @websocket_api.async_response
    async def action_command(hass, connection, msg):
        manager = hass.data.get(DOMAIN)
        if manager is None:
            connection.send_error(msg["id"], "not_ready", "请先添加 AI Tools 集成")
            return
        try:
            result = await async_action(manager, msg["action"], msg["payload"])
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_value", models.sanitize(str(err)))
        except Exception as err:
            _LOGGER.error("管理操作失败: %s", type(err).__name__)
            connection.send_error(msg["id"], "operation_failed", "操作失败，请核对当前状态后重试")
        else:
            connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, data_command)
    websocket_api.async_register_command(hass, action_command)
