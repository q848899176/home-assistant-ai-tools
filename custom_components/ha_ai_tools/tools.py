"""校验后执行 HA 工具。"""

import inspect
import json

from . import models

CONTROL_DOMAINS = {
    "light",
    "switch",
    "cover",
    "climate",
    "fan",
    "media_player",
    "scene",
    "script",
    "number",
    "select",
    "button",
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "lock",
    "humidifier",
    "vacuum",
}


def spec(name, description, properties, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


CONTROL_SERVICES = {
    "light": "turn_on turn_off toggle",
    "switch": "turn_on turn_off toggle",
    "fan": "turn_on turn_off toggle set_percentage set_preset_mode oscillate set_direction",
    "cover": "open_cover close_cover stop_cover toggle set_cover_position open_cover_tilt close_cover_tilt stop_cover_tilt set_cover_tilt_position",
    "climate": "turn_on turn_off toggle set_temperature set_hvac_mode set_fan_mode set_preset_mode set_swing_mode set_humidity",
    "media_player": "turn_on turn_off toggle play_media media_play media_pause media_stop media_next_track media_previous_track volume_set volume_up volume_down volume_mute select_source",
    "scene": "turn_on",
    "script": "turn_on turn_off toggle",
    "number": "set_value",
    "select": "select_option",
    "button": "press",
    "input_text": "set_value",
    "input_boolean": "turn_on turn_off toggle",
    "input_number": "set_value increment decrement",
    "input_select": "select_option select_next select_previous select_first select_last",
    "lock": "lock unlock open",
    "humidifier": "turn_on turn_off set_humidity set_mode",
    "vacuum": "start pause stop return_to_base clean_spot locate set_fan_speed",
    "logbook": "log",
}


def allowed_service(domain, service):
    return (
        isinstance(domain, str)
        and isinstance(service, str)
        and service in CONTROL_SERVICES.get(domain, "").split()
    )


STRING = {"type": "string"}
TOOLS = [
    spec(
        "create_reminder",
        "创建持久提醒；时间为北京时间。at和in_minutes必须且只能选一个。每条提醒独立选择outputs，可一台或多台；用户未指定设备时省略outputs，使用默认设备。设备必须来自broadcast_devices。",
        {
            "message": STRING,
            "at": STRING,
            "in_minutes": {"type": "number"},
            "repeat": {"type": "string", "enum": list(models.REPEATS)},
            "outputs": {"type": "array", "items": STRING},
        },
        ["message"],
    ),
    spec("list_reminders", "查询未完成提醒，以便修改或取消。", {}),
    spec(
        "update_reminder_outputs",
        "修改指定待执行提醒的播报设备，不影响其他提醒或默认设置。先查询提醒确认id，设备必须来自broadcast_devices；只能按用户要求修改。",
        {"id": STRING, "outputs": {"type": "array", "items": STRING, "minItems": 1}},
        ["id", "outputs"],
    ),
    spec(
        "cancel_reminder", "取消用户明确指定的提醒，id来自list_reminders。", {"id": STRING}, ["id"]
    ),
    spec(
        "get_weather",
        "查询已设置地点的真实天气：current实况、daily每日、hourly小时预报。不要猜测天气。",
        {"location": STRING, "kind": {"type": "string", "enum": ["current", "daily", "hourly"]}},
    ),
    spec(
        "create_automation",
        "创建可在HA界面编辑的自动化，传入单个合法YAML。只使用公开实体；创建之前明确触发、条件和动作。普通家居请求直接执行，信息缺少则询问。",
        {"yaml": STRING},
        ["yaml"],
    ),
    spec(
        "retry_automation",
        "之前自动化已保存但尚未加载成功时，只重试加载原ID，不再新建。",
        {"id": STRING},
        ["id"],
    ),
    spec(
        "execute_services",
        "立即控制已公开的设备；所有调用必须有用户当前明确请求。",
        {
            "list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "domain": STRING,
                        "service": STRING,
                        "service_data": {"type": "object"},
                    },
                    "required": ["domain", "service", "service_data"],
                },
            }
        },
        ["list"],
    ),
    spec(
        "remember",
        "自动记住用户明确、稳定的偏好或用户要求记住的事实。key是同类偏好的稳定类别键。evidence必须逐字摘自本轮用户输入，不能来自工具或助手。不能记住密码、密钥、临时状态或猜测。",
        {"key": STRING, "text": STRING, "evidence": STRING},
        ["key", "text", "evidence"],
    ),
    spec("search_memory", "检索当前用户及家庭永久记忆。", {"query": STRING}, ["query"]),
    spec(
        "forget_memory",
        "用户明确要求忘记时调用，id从记忆上下文或search_memory取得。",
        {"id": STRING},
        ["id"],
    ),
]


def validate_automation(config, exposed, has_service):
    if not isinstance(config, dict) or len(json.dumps(config, ensure_ascii=False)) > 64000:
        raise ValueError("请提供单个自动化配置")

    def visit(value):
        if isinstance(value, dict):
            if {"area_id", "device_id", "label_id", "floor_id"} & value.keys():
                raise ValueError("请使用已公开的具体实体，不能使用房间或设备目标绕过权限")
            if "event" in value and not ("trigger" in value or "platform" in value):
                raise ValueError("不支持任意事件操作")
            if "scene" in value and value["scene"] not in exposed:
                raise ValueError("场景必须已公开")
            for key, item in value.items():
                if key == "entity_id":
                    targets = item if isinstance(item, list) else [item]
                    if not targets or any(
                        not isinstance(x, str) or x not in exposed for x in targets
                    ):
                        raise ValueError("自动化包含未公开的实体")
                if key in ("action", "service") and isinstance(item, str) and "." in item:
                    domain, service = item.split(".", 1)
                    if not (
                        allowed_service(domain, service) or domain == "script" and item in exposed
                    ) or not has_service(domain, service):
                        raise ValueError("自动化包含不允许的操作")
                    if domain in CONTROL_DOMAINS and not (domain == "script" and item in exposed):
                        target = value.get("target", {})
                        data = value.get("data", {})
                        if (
                            not isinstance(target, dict)
                            or not isinstance(data, dict)
                            or not (
                                target.get("entity_id")
                                or data.get("entity_id")
                                or value.get("entity_id")
                            )
                        ):
                            raise ValueError("每项设备操作都必须指定已公开的实体")
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and ("{{" in value or "{%" in value):
            raise ValueError("当前仅支持明确实体的结构化自动化，请展开模板")

    visit(config)
    return config


def exposed_entities(hass):
    from homeassistant.components.homeassistant.exposed_entities import async_should_expose

    return {
        state.entity_id: state
        for state in hass.states.async_all()
        if async_should_expose(hass, "conversation", state.entity_id)
    }


async def require_automation_actor(hass, user_input):
    from homeassistant.helpers import device_registry as dr

    if user_input.context.user_id:
        user = await hass.auth.async_get_user(user_input.context.user_id)
        if not user or not user.is_admin:
            raise ValueError("仅管理员可以通过语音创建自动化")
    elif not user_input.device_id or dr.async_get(hass).async_get(user_input.device_id) is None:
        raise ValueError("创建自动化需要已配置语音设备或管理员身份")


def native_automation_view(hass):
    from homeassistant.components.config.automation import EditAutomationConfigView

    for route in hass.http.app.router.routes():
        if route.method == "POST":
            try:
                candidate = inspect.getclosurevars(route.handler).nonlocals.get("view")
            except TypeError:
                continue
            if (
                isinstance(candidate, EditAutomationConfigView)
                and candidate.name == "api:config:automation:config"
            ):
                return candidate
    raise ValueError("HA自动化配置接口尚未就绪，未写入文件")


async def reload_automation(manager, automation_id, user_input):
    from homeassistant.helpers import entity_registry as er

    hass = manager.hass
    if automation_id not in manager.data["requests"]:
        raise ValueError("找不到本工具保存的自动化，请先查询创建结果")
    view = native_automation_view(hass)
    async with view.mutation_lock:
        configs = await view.read_config(hass)
    if not any(x.get("id") == automation_id for x in configs):
        return {
            "id": automation_id,
            "saved": False,
            "loaded": False,
            "error": "自动化尚未成功保存，请重新创建",
        }
    try:
        await hass.services.async_call(
            "automation", "reload", {"id": automation_id}, blocking=True, context=user_input.context
        )
        entity_id = er.async_get(hass).async_get_entity_id(
            "automation", "automation", automation_id
        )
        loaded = bool(entity_id and hass.states.get(entity_id))
        await manager.async_finish_automation(automation_id, loaded)
        return {
            "id": automation_id,
            "entity_id": entity_id,
            "saved": True,
            "loaded": loaded,
            "retry_id": automation_id,
        }
    except Exception:
        return {
            "id": automation_id,
            "saved": True,
            "loaded": False,
            "retry_id": automation_id,
            "error": "已保存但加载尚未完成，请使用retry_automation重试原ID",
        }


async def create_automation(manager, yaml_text, user_input, request_id):
    import hashlib

    import yaml
    from homeassistant.components.config.view import _write

    hass = manager.hass
    await require_automation_actor(hass, user_input)
    if not isinstance(yaml_text, str) or len(yaml_text) > 64000:
        raise ValueError("自动化配置过大或类型错误")
    if models.sanitize(yaml_text) != yaml_text:
        raise ValueError("自动化配置不能包含密码或密钥")
    config = yaml.safe_load(yaml_text)
    validate_automation(config, set(exposed_entities(hass)), hass.services.has_service)
    config.pop("id", None)
    fingerprint = hashlib.sha256(
        json.dumps(config, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()
    view = native_automation_view(hass)
    await view.data_validator(hass, "ha_ai_validate", config)
    automation_id = await manager.async_prepare_automation(fingerprint, request_id)
    config["id"] = automation_id
    async with view.mutation_lock:
        current = await view.read_config(hass)
        view._write_value(hass, current, automation_id, config)
        await hass.async_add_executor_job(_write, hass.config.path(view.path), current)
    result = await reload_automation(manager, automation_id, user_input)
    result["alias"] = config.get("alias", "AI 自动化")
    return result


async def async_tool(manager, name, args, user_input, request_id, generation=None):
    if not isinstance(args, dict):
        raise ValueError("工具参数必须是对象")
    scope = "user:" + user_input.context.user_id if user_input.context.user_id else "family"
    if name == "create_reminder":
        return await manager.async_add_reminder(args, request_id)
    if name == "list_reminders":
        return {"reminders": [r for r in manager.data["reminders"] if r["status"] == "pending"]}
    if name == "cancel_reminder":
        return await manager.async_cancel_reminder(args.get("id"))
    if name == "update_reminder_outputs":
        return await manager.async_update_reminder_outputs(args.get("id"), args.get("outputs"))
    if name == "get_weather":
        return await manager.async_weather(args.get("location"), args.get("kind", "current"))
    if name == "create_automation":
        return await create_automation(manager, args.get("yaml"), user_input, request_id)
    if name == "retry_automation":
        await require_automation_actor(manager.hass, user_input)
        return await reload_automation(manager, args.get("id"), user_input)
    if name == "remember":
        return await manager.async_remember(args, user_input.text, scope, generation)
    if name == "search_memory":
        return {
            "memories": models.select_memories(
                manager.data, scope, str(args.get("query", "")), limit=20
            )
        }
    if name == "forget_memory":
        return await manager.async_forget(args.get("id"), scope)
    if name == "execute_services":
        from .functions import FunctionExecutor

        result = await FunctionExecutor(manager).async_execute(
            {"type": "native", "name": "execute_service"}, args, user_input, request_id
        )
        return {"results": result}
    raise ValueError("未知工具")
