"""管理员管理接口。"""

import json
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
                    "aliases": er.async_get_entity_aliases(hass, entity) if entity else [],
                    "area": area.name if area else "未分配房间",
                    "available": state.state not in ("unavailable", "unknown"),
                }
            )
    return outputs


def snapshot(manager):
    from homeassistant.helpers import entity_registry as er

    from .weather import weather_sources

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
            weather.append(
                {
                    "entity_id": state.entity_id,
                    "name": state.name,
                    "state": state.state,
                    "fallback_entities": weather_sources(hass, state.entity_id)[1:],
                }
            )
    from .provider import list_connections, list_profiles
    from .speech.api import snapshot as speech_snapshot

    profiles = list_profiles(manager.entry)
    for profile in profiles:
        if _function_credentials(profile.get("functions")):
            profile["functions"] = ""
            profile["functions_redacted"] = True

    return {
        "connections": list_connections(manager.entry),
        "profiles": profiles,
        "native_connection": bool(list_connections(manager.entry)),
        "migration_error": getattr(manager, "migration_error", ""),
        "settings": {
            k: v
            for k, v in manager.settings.items()
            if k in {"source_entity", "outputs", "locations"}
        },
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
        "speech": speech_snapshot(manager),
    }


def _function_credentials(raw):
    """Never return administrator HTTP credentials as ordinary page state."""
    import yaml

    if isinstance(raw, str):
        try:
            raw = yaml.safe_load(raw)
        except yaml.YAMLError:
            return True

    def visit(value):
        if isinstance(value, dict):
            return any(
                str(key).lower()
                in {
                    "headers",
                    "authentication",
                    "username",
                    "password",
                    "api_key",
                    "token",
                    "secret",
                    "cookie",
                }
                or visit(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(visit(item) for item in value)
        return isinstance(value, str) and bool(models.SENSITIVE.search(value))

    return visit(raw)


async def async_action(manager, action, payload, *, actor=None):
    from .speech.api import ACTIONS
    from .speech.api import async_action as speech_action

    if action in ACTIONS:
        return await speech_action(manager, action, payload, actor=actor)
    if action == "preview_legacy":
        from .functions import compile_functions
        from .provider import get_provider, validate_profile

        legacy, _ = get_provider(manager.hass, payload.get("source_entity"))
        entries = [s for s in legacy.subentries.values() if s.subentry_type == "conversation"]
        profiles = []
        for subentry in entries or [None]:
            profile = validate_profile(dict(subentry.data) if subentry else dict(legacy.options))
            profiles.append(
                {
                    "name": subentry.title if subentry else legacy.title,
                    "chat_model": profile["chat_model"],
                    "max_tokens": profile["max_tokens"],
                    "function_count": len(compile_functions(profile["functions"])),
                    "prompt_configured": bool(profile["prompt"]),
                }
            )
        component = manager.hass.data.get("automation")
        references = [
            entity.entity_id
            for entity in getattr(component, "entities", [])
            if "extended_openai_conversation"
            in json.dumps(getattr(entity, "raw_config", {}), default=str)
        ]
        return {
            "name": legacy.title,
            "profiles": profiles,
            "references": references,
            "warnings": [
                "原提示词和参数将保留，同时遵守北京时间、真实工具结果和记忆归属约束。",
                "旧集成配置会保留作回退，引用旧服务或事件的自动化不会自动修改。",
            ],
        }
    if action in {"save_connection", "save_profile", "import_legacy", "test_connection"}:
        from .provider import (
            async_import_legacy,
            async_save_connection,
            async_save_profile,
            async_test_connection,
        )

        if action == "save_connection":
            return await async_save_connection(manager, payload)
        if action == "save_profile":
            return await async_save_profile(manager, payload)
        if action == "import_legacy":
            result = await async_import_legacy(manager, payload.get("source_entity"))
            manager.migration_error = ""
            manager.hass.config_entries.async_update_entry(
                manager.entry, data={**manager.entry.data, "native_migration_completed": True}
            )
            return result
        return await async_test_connection(
            manager, payload.get("connection_id"), payload.get("profile_id")
        )
    if action == "validate_functions":
        from .functions import compile_functions
        from .provider import get_profile
        from .tools import TOOLS

        reserved = [t["function"]["name"] for t in TOOLS]
        raw = payload.get("functions", "")
        if "functions" not in payload and payload.get("profile_id"):
            raw = get_profile(manager.entry, payload["profile_id"])["functions"]
        definitions = compile_functions(raw, reserved=reserved)
        return {
            "valid": True,
            "count": len(definitions),
            "names": [d["spec"]["name"] for d in definitions],
        }
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
        return await manager.async_update_reminder_outputs(
            payload.get("id"), payload.get("outputs")
        )
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
        runtime = getattr(manager, "speech", None)
        if runtime is None or not getattr(runtime, "testing", None) or not payload.get("test_id"):
            raise ValueError("请先选择Nabu并建立专用测试会话；不会向全屋默认设备测试播报")
        run = runtime.store.data["test_runs"].get(payload["test_id"], {})
        outputs = runtime.testing.validate_targets(payload["test_id"], payload.get("outputs", run.get("targets", [])))
        return await manager.async_broadcast("这是 AI 工具的播报测试。", outputs, test_id=payload["test_id"])
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
            result = await async_action(manager, msg["action"], msg["payload"], actor=connection.user.id)
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_value", models.sanitize(str(err)))
        except Exception as err:
            _LOGGER.error("管理操作失败: %s", type(err).__name__)
            connection.send_error(msg["id"], "operation_failed", "操作失败，请核对当前状态后重试")
        else:
            connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, data_command)
    websocket_api.async_register_command(hass, action_command)
