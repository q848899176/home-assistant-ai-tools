"""Bounded HA-native and HTTP function execution; upstream attribution in NOTICE."""

import asyncio
import copy
import json
import logging
import math
from contextvars import ContextVar
from datetime import datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
import yaml
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.core import SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import target as target_helper
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.helpers.template import Template

from .. import tools
from . import PROTECTED, _bounded, _function, allowed_origins, origin

_LOGGER = logging.getLogger(__name__)
_BEIJING = ZoneInfo("Asia/Shanghai")
_MAX_RESPONSE = 128000
_VISIBLE = ContextVar("ha_ai_tools_function_visible", default=None)
_READ_SERVICES = {("weather", "get_forecasts"), ("calendar", "get_events")}
_ENTITY_DOMAINS = tools.CONTROL_DOMAINS | {
    "alarm_control_panel",
    "automation",
    "calendar",
    "camera",
    "counter",
    "date",
    "datetime",
    "input_button",
    "input_datetime",
    "lawn_mower",
    "remote",
    "siren",
    "text",
    "time",
    "timer",
    "todo",
    "valve",
    "water_heater",
}
_FORBIDDEN_SERVICES = {
    "reload",
    "reload_core_config",
    "reload_config_entry",
    "restart",
    "record",
    "snapshot",
}
_TARGET_KEYS = {"entity_id", "device_id", "area_id", "floor_id", "label_id"}


class FunctionExecutor:
    """Execute only validated administrator configurations with the current actor."""

    def __init__(self, manager):
        self.manager = manager
        self.hass = manager.hass
        if not hasattr(manager, "function_tasks"):
            manager.function_tasks = set()

    def _render(self, value, variables, parse_result=False):
        if isinstance(value, Template):
            template = Template(value.template, self.hass)
        else:
            template = Template(value, self.hass)
        try:
            info = template.async_render_to_info(variables, parse_result=parse_result, strict=True)
            visible = _VISIBLE.get()
            if visible is not None:
                referenced = set(info.entities)
                if info.all_states:
                    referenced.update(state.entity_id for state in self.hass.states.async_all())
                for domain in info.domains:
                    referenced.update(
                        state.entity_id for state in self.hass.states.async_all(domain)
                    )
                if referenced - visible:
                    raise ValueError("函数模板包含未公开或无权读取的实体")
            return info.result()
        except ValueError:
            raise
        except Exception as err:
            raise ValueError("函数模板渲染失败，请检查变量与模板") from err

    def _render_tree(self, value, variables):
        if isinstance(value, (str, Template)):
            if isinstance(value, Template) or "{{" in value or "{%" in value:
                return self._render(value, variables, True)
            return value
        if isinstance(value, list):
            return [self._render_tree(item, variables) for item in value]
        if isinstance(value, dict):
            return {key: self._render_tree(item, variables) for key, item in value.items()}
        return value

    def _value_template(self, template, value, variables):
        merged = dict(variables, value=value)
        try:
            merged["value_json"] = json.loads(value) if isinstance(value, str) else value
        except TypeError, ValueError:
            pass
        return self._render(template, merged, False)

    async def _actor(self, user_input):
        context = getattr(user_input, "context", None)
        user_id = getattr(context, "user_id", None)
        if user_id:
            user = await self.hass.auth.async_get_user(user_id)
            if user is None or not getattr(user, "is_active", True):
                raise ValueError("当前用户身份无效")
            return user
        device_id = getattr(user_input, "device_id", None)
        if not device_id or dr.async_get(self.hass).async_get(device_id) is None:
            raise ValueError("执行函数需要可信用户或已登记语音设备")
        return None

    async def _entities(self, entity_ids, user_input, control=False):
        if not isinstance(entity_ids, (list, set, tuple)) or not entity_ids:
            raise ValueError("必须明确指定已公开实体")
        exposed = tools.exposed_entities(self.hass)
        actor = await self._actor(user_input)
        policy = POLICY_CONTROL if control else POLICY_READ
        for entity_id in entity_ids:
            if not isinstance(entity_id, str) or entity_id not in exposed:
                raise ValueError("操作包含未公开或不存在的实体")
            if actor is not None and not actor.permissions.check_entity(entity_id, policy):
                raise ValueError("当前用户没有实体操作权限")
        return actor

    async def _visible(self, user_input):
        actor = await self._actor(user_input)
        return {
            entity_id
            for entity_id in tools.exposed_entities(self.hass)
            if actor is None or actor.permissions.check_entity(entity_id, POLICY_READ)
        }

    async def _service_plan(self, call, user_input):
        if not isinstance(call, dict):
            raise ValueError("服务调用参数必须是对象")
        domain, service = call.get("domain"), call.get("service")
        if not isinstance(domain, str) or not isinstance(service, str):
            raise ValueError("服务domain与service必须是字符串")
        read_service = (domain, service) in _READ_SERVICES
        ordinary = domain in _ENTITY_DOMAINS and service not in _FORBIDDEN_SERVICES
        if domain == "scene" and service == "apply":
            ordinary = False
        if (
            not ordinary
            and not read_service
            and not (domain == "homeassistant" and service in {"turn_on", "turn_off", "toggle"})
        ):
            raise ValueError("不允许的设备服务")
        if not self.hass.services.has_service(domain, service):
            raise ValueError("HA服务不存在")
        source = call.get("service_data", call.get("data", {}))
        if not isinstance(source, dict) or not isinstance(call.get("target", {}), dict):
            raise ValueError("服务data与target必须是对象")
        data = copy.deepcopy(source)
        target = dict(call.get("target", {}))
        for key in _TARGET_KEYS:
            if key in call:
                target[key] = call[key]
            if key in data:
                if key in target and target[key] != data[key]:
                    raise ValueError("服务目标存在冲突")
                target[key] = data.pop(key)
        if domain == "script" and service not in {"turn_on", "turn_off", "toggle"}:
            target["entity_id"] = f"script.{service}"
        if not target or any(key not in _TARGET_KEYS for key in target):
            raise ValueError("设备操作必须指定明确目标")
        for key, value in target.items():
            if isinstance(value, str):
                value = [part.strip() for part in value.split(",")]
            if (
                not isinstance(value, list)
                or not value
                or any(not isinstance(item, str) or item in {"all", "none", ""} for item in value)
            ):
                raise ValueError("设备目标参数无效")
            target[key] = value
        try:
            selection = target_helper.async_extract_referenced_entity_ids(
                self.hass, target_helper.TargetSelection(target), primary_entities_only=False
            )
            if any(
                getattr(selection, name, set())
                for name in ("missing_areas", "missing_devices", "missing_floors", "missing_labels")
            ):
                raise ValueError("指定房间或设备不存在")
            entities = selection.referenced | selection.indirectly_referenced
        except (KeyError, TypeError) as err:
            raise ValueError("无法解析设备或房间目标") from err
        # Send concrete entities only, so a registry update cannot broaden the target later.
        await self._entities(entities, user_input, control=not read_service)
        if domain != "homeassistant" and any(
            entity.split(".", 1)[0] != domain for entity in entities
        ):
            # Area targets routinely contain multiple domains; restrict to the requested domain,
            # after checking every resolved entity so hidden targets never pass preflight.
            entities = {entity for entity in entities if entity.split(".", 1)[0] == domain}
        if not entities:
            raise ValueError("目标没有匹配的设备实体")
        data["entity_id"] = sorted(entities)
        return domain, service, data

    async def _call_service(self, plan, user_input):
        domain, service, data = plan
        supports = self.hass.services.supports_response(domain, service)
        response = await self.hass.services.async_call(
            domain,
            service,
            data,
            blocking=True,
            context=user_input.context,
            return_response=supports != SupportsResponse.NONE,
        )
        result = {"success": True, "entity_id": data["entity_id"]}
        if response is not None:
            result["response"] = response
        return result

    async def async_execute(self, function_config, args, user_input, request_id=None):
        """Run a function, or schedule its delay without claiming completion."""
        if not isinstance(args, dict) or PROTECTED.intersection(args):
            raise ValueError("函数参数必须是对象且不能覆盖系统保留变量")
        _bounded(args)
        _bounded(function_config)
        _function(function_config)
        await self._actor(user_input)
        config, variables = copy.deepcopy(function_config), copy.deepcopy(args)
        delay = variables.pop("delay", None)
        if delay is not None:
            try:
                seconds = cv.time_period(delay).total_seconds()
                if not math.isfinite(seconds) or not 0 <= seconds <= 86400:
                    raise ValueError
            except Exception as err:
                raise ValueError("delay必须是0至86400秒内的HA时间段") from err

            async def delayed():
                await asyncio.sleep(seconds)
                return await self._run(config, variables, user_input, request_id)

            task = self.hass.async_create_background_task(delayed(), "ha_ai_tools delayed function")
            self.manager.function_tasks.add(task)

            def completed(done):
                self.manager.function_tasks.discard(done)
                if not done.cancelled() and done.exception() is not None:
                    # Do not log exception text; it can contain configured credentials or private data.
                    _LOGGER.error("延迟函数执行失败（%s）", type(done.exception()).__name__)

            task.add_done_callback(completed)
            return {"scheduled": True, "completed": False, "delay_seconds": seconds}
        return await self._run(config, variables, user_input, request_id)

    async def _run(self, config, variables, user_input, request_id):
        token = _VISIBLE.set(await self._visible(user_input))
        try:
            async with asyncio.timeout(120):
                return await self._execute(config, variables, user_input, request_id)
        except TimeoutError as err:
            raise ValueError("函数执行超时") from err
        finally:
            _VISIBLE.reset(token)

    async def _execute(self, config, variables, user_input, request_id):
        kind = config["type"]
        if kind == "native":
            result = await self._native(config, variables, user_input, request_id)
        elif kind == "template":
            result = self._render(
                config["value_template"], variables, config.get("parse_result", False)
            )
        elif kind == "script":
            result = await self._script(config, variables, user_input)
        elif kind in {"rest", "scrape"}:
            result = await self._http(config, variables)
        elif kind == "composite":
            result = None
            for step in config["sequence"]:
                result = await self._execute(step, variables, user_input, request_id)
                if variable := step.get("response_variable"):
                    if variable in PROTECTED:
                        raise ValueError("响应变量不能覆盖系统保留变量")
                    variables[variable] = result
        else:
            from .sqlite import async_query

            visible = await self._visible(user_input)
            helpers = {
                "is_exposed": lambda entity: entity in visible,
                "is_exposed_entity_in_query": lambda query: any(
                    "'" + entity.replace("'", "''") + "'" in query for entity in visible
                ),
                "exposed_entities": [{"entity_id": entity} for entity in sorted(visible)],
                "raise": _raise,
            }
            query = self._render(
                config.get("query", "{{ query }}"), dict(variables, **helpers), False
            )
            actor = await self._actor(user_input)
            result = await async_query(
                self.hass, config, query, visible, bool(actor and actor.is_admin)
            )
        if len(json.dumps(result, ensure_ascii=False, default=str)) > _MAX_RESPONSE:
            raise ValueError("函数结果超出128KB上限，请缩小查询范围")
        return result

    async def _native(self, config, args, user_input, request_id):
        name = config["name"]
        if name in {"execute_service", "execute_service_single"}:
            calls = args.get("list", []) if name == "execute_service" else [args]
            if not isinstance(calls, list) or not calls or len(calls) > 50:
                raise ValueError("服务调用列表必须包含1至50项")
            plans = [await self._service_plan(call, user_input) for call in calls]
            results = [await self._call_service(plan, user_input) for plan in plans]
            return results if name == "execute_service" else results[0]
        if name == "add_automation":
            text = args.get("automation_config", args.get("yaml"))
            result = await tools.create_automation(self.manager, text, user_input, request_id)
            if result.get("saved"):
                from ..models import sanitize_data

                automation_config = yaml.safe_load(text)
                if isinstance(automation_config, dict):
                    automation_config["id"] = result["id"]
                    self.hass.bus.async_fire(
                        "automation_registered_via_ha_ai_tools",
                        sanitize_data(
                            {
                                "automation_config": automation_config,
                                "raw_config": yaml.safe_dump(
                                    automation_config, allow_unicode=True, sort_keys=False
                                ),
                                "saved": result["saved"],
                                "loaded": result["loaded"],
                                "id": result["id"],
                            }
                        ),
                        context=user_input.context,
                    )
            return result
        if name == "get_user_from_user_id":
            actor = await self._actor(user_input)
            return {"name": actor.name if actor and actor.name else "Unknown"}
        if name == "get_energy":
            from homeassistant.components import energy

            manager = await energy.async_get_manager(self.hass)
            data = manager.data
            visible = await self._visible(user_input)

            def check(item):
                if isinstance(item, dict):
                    for key, value in item.items():
                        check(key)
                        check(value)
                elif isinstance(item, list):
                    for value in item:
                        check(value)
                elif isinstance(item, str) and _entity_reference(item) and item not in visible:
                    raise ValueError("能源配置包含未公开或无权读取的实体")

            check(data)
            return copy.deepcopy(data)
        from homeassistant.components import recorder

        if name == "get_history":
            from homeassistant.components.recorder import history

            ids = args.get("entity_ids", [])
            if isinstance(ids, str):
                ids = [part.strip() for part in ids.split(",")]
            await self._entities(ids, user_input)
            start, end = _period(args, timedelta(days=1), 31)
            result = await recorder.get_instance(self.hass).async_add_executor_job(
                history.get_significant_states,
                self.hass,
                start,
                end,
                list(ids),
                None,
                args.get("include_start_time_state", True),
                args.get("significant_changes_only", True),
                args.get("minimal_response", True),
                args.get("no_attributes", True),
            )
            return [
                [
                    _local_times(state.as_dict() if hasattr(state, "as_dict") else state)
                    for state in states
                ]
                for states in result.values()
            ]
        from homeassistant.components.recorder import statistics

        ids = args.get("statistic_ids", [])
        await self._entities(ids, user_input)
        start, end = _period(args, timedelta(days=1), 366)
        period = args.get("period", "day")
        types = set(args.get("types", ["change"]))
        if (
            period not in {"5minute", "hour", "day", "week", "month", "year"}
            or not types
            or types - {"change", "last_reset", "max", "mean", "min", "state", "sum"}
        ):
            raise ValueError("统计周期或类型无效")
        result = await recorder.get_instance(self.hass).async_add_executor_job(
            statistics.statistics_during_period,
            self.hass,
            start,
            end,
            set(ids),
            period,
            args.get("units"),
            types,
        )
        return _local_times(result)

    async def _script(self, config, variables, user_input):
        from homeassistant.helpers.script import Script, async_validate_actions_config

        # Preflight concrete targets before HA Script starts any side effect. The service
        # and target are frozen; data/response variables retain native script behavior.
        async def prepare(sequence, scope):
            prepared = []
            for original in sequence:
                step = copy.deepcopy(original)
                await self._script_read_guards(step, scope, user_input)
                if "variables" in step:
                    if not isinstance(step["variables"], dict) or PROTECTED.intersection(
                        step["variables"]
                    ) - {"_function_result"}:
                        raise ValueError("脚本变量不能覆盖系统保留变量")
                    for variable, value in step["variables"].items():
                        rendered, known = checked(value, scope)
                        if known:
                            scope[variable] = rendered
                if "action" in step or "service" in step:
                    key = "action" if "action" in step else "service"
                    service = self._render_tree(step[key], scope)
                    if not isinstance(service, str) or "." not in service:
                        raise ValueError("脚本服务无法安全解析")
                    domain, name = service.split(".", 1)
                    data = copy.deepcopy(step.get("data", step.get("data_template", {})))
                    checked(data, scope)
                    for target_key in _TARGET_KEYS.intersection(data):
                        data[target_key] = self._render_tree(data[target_key], scope)
                    target = self._render_tree(step.get("target", {}), scope)
                    plan = await self._service_plan(
                        {"domain": domain, "service": name, "data": data, "target": target},
                        user_input,
                    )
                    step[key] = service
                    step.pop("data_template", None)
                    step["data"] = plan[2]
                    step.pop("target", None)
                elif "event" in step or "device_id" in step or "scene" in step:
                    raise ValueError("脚本需要明确的已公开实体服务操作")
                elif "choose" in step:
                    for option in step["choose"]:
                        option["sequence"] = await prepare(option["sequence"], dict(scope))
                    if "default" in step:
                        step["default"] = await prepare(step["default"], dict(scope))
                elif "if" in step:
                    step["then"] = await prepare(step.get("then", []), dict(scope))
                    if "else" in step:
                        step["else"] = await prepare(step["else"], dict(scope))
                elif "repeat" in step:
                    step["repeat"]["sequence"] = await prepare(
                        step["repeat"]["sequence"], dict(scope)
                    )
                elif "parallel" in step:
                    step["parallel"] = await prepare(step["parallel"], dict(scope))
                elif "sequence" in step:
                    step["sequence"] = await prepare(step["sequence"], dict(scope))
                prepared.append(step)
            return prepared

        def checked(value, scope):
            try:
                return self._render_tree(value, scope), True
            except ValueError as err:
                from homeassistant.exceptions import TemplateError
                from jinja2 import UndefinedError

                cause = err.__cause__
                if not isinstance(cause, TemplateError) or not isinstance(
                    cause.__cause__, UndefinedError
                ):
                    raise
                # Native script responses may be unavailable during preflight. Defer
                # pure value expressions, never deferred HA state or registry access.
                text = str(value)
                if any(
                    word in text
                    for word in (
                        "states",
                        "state_attr",
                        "is_state",
                        "expand",
                        "entities",
                        "device_attr",
                        "area_id",
                        "device_id",
                        "closest",
                        "distance",
                    )
                ):
                    raise ValueError("脚本动态实体模板无法安全检查") from err
                return value, False

        sequence = await prepare(config["sequence"], dict(variables))
        from homeassistant.components.script.config import SCRIPT_ENTITY_SCHEMA

        sequence = SCRIPT_ENTITY_SCHEMA({"sequence": sequence})["sequence"]
        sequence = await async_validate_actions_config(self.hass, sequence)
        script = Script(
            self.hass,
            sequence,
            "AI Tools function",
            "ha_ai_tools",
            logger=_LOGGER,
            log_exceptions=False,
        )
        try:
            async with asyncio.timeout(120):
                result = await script.async_run(run_variables=variables, context=user_input.context)
        except asyncio.CancelledError:
            await script.async_stop()
            raise
        except TimeoutError as err:
            await script.async_stop()
            raise ValueError("HA脚本执行超时") from err
        if result is None:
            return {"completed": True}
        return result.variables.get(
            "_function_result",
            result.service_response if result.service_response is not None else "Success",
        )

    async def _script_read_guards(self, value, scope, user_input):
        if isinstance(value, (str, Template)):
            self._render(value, scope, True)
        elif isinstance(value, list):
            for item in value:
                await self._script_read_guards(item, scope, user_input)
        elif isinstance(value, dict):
            if "condition" in value and "entity_id" in value:
                entities = self._render_tree(value["entity_id"], scope)
                if isinstance(entities, str):
                    entities = [entity.strip() for entity in entities.split(",")]
                await self._entities(entities, user_input)
            for key in ("wait_template", "value_template", "if", "conditions", "while", "until"):
                if key in value:
                    await self._script_read_guards(value[key], scope, user_input)
            if (
                isinstance(value.get("condition"), Template)
                or isinstance(value.get("condition"), str)
                and "{{" in value["condition"]
            ):
                self._render(value["condition"], scope, True)
            if "wait_for_trigger" in value:
                triggers = value["wait_for_trigger"]
                if not isinstance(triggers, list):
                    triggers = [triggers]
                for trigger in triggers:
                    kind = trigger.get("trigger", trigger.get("platform"))
                    if kind not in {
                        "state",
                        "numeric_state",
                        "time",
                        "time_pattern",
                        "sun",
                        "template",
                    }:
                        raise ValueError("脚本等待触发器必须限定已公开实体")
                    if "entity_id" in trigger:
                        entities = self._render_tree(trigger["entity_id"], scope)
                        await self._entities(
                            [entities] if isinstance(entities, str) else entities, user_input
                        )
            for item in value.values():
                if isinstance(item, (list, dict)):
                    await self._script_read_guards(item, scope, user_input)

    async def _http(self, config, variables):
        allowed = allowed_origins(config)
        url = (
            self._render(config["resource_template"], variables, False)
            if "resource_template" in config
            else config["resource"]
        )
        if origin(url) not in allowed:
            raise ValueError("HTTP地址超出管理员配置的主机范围")
        headers = self._render_tree(config.get("headers", {}), variables)
        headers = {key: str(value) for key, value in headers.items()}
        params = self._render_tree(config.get("params", {}), variables)
        if params:
            url = str(httpx.URL(url).copy_merge_params(params))
        payload = (
            self._render(config["payload_template"], variables, False)
            if "payload_template" in config
            else config.get("payload")
        )
        auth = None
        if config.get("username") is not None:
            auth_cls = (
                httpx.DigestAuth
                if config.get("authentication", "basic") == "digest"
                else httpx.BasicAuth
            )
            auth = auth_cls(config["username"], config.get("password", ""))
        client = get_async_client(self.hass, verify_ssl=config.get("verify_ssl", True))
        try:
            async with asyncio.timeout(config.get("timeout", 10)):
                for _ in range(4):
                    async with client.stream(
                        config.get("method", "GET"),
                        url,
                        headers=headers,
                        content=payload,
                        auth=auth,
                        timeout=config.get("timeout", 10),
                        follow_redirects=False,
                    ) as response:
                        if response.is_redirect:
                            url = urljoin(url, response.headers.get("location", ""))
                            if origin(url) not in allowed:
                                raise ValueError("HTTP重定向超出管理员配置的主机范围")
                            continue
                        if not response.is_success:
                            raise ValueError(f"HTTP请求失败，状态码{response.status_code}")
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 2000000:
                                raise ValueError("HTTP响应超出2MB上限")
                        value = bytes(content).decode(
                            config.get("encoding") or response.encoding or "utf-8"
                        )
                        if config["type"] == "rest" and response.headers.get(
                            "content-type", ""
                        ).startswith(
                            (
                                "application/xml",
                                "text/xml",
                                "application/rss+xml",
                                "application/xhtml+xml",
                            )
                        ):
                            import xmltodict

                            try:
                                value = json.dumps(
                                    xmltodict.parse(value, disable_entities=True),
                                    ensure_ascii=False,
                                )
                            except Exception as err:
                                raise ValueError("REST响应XML解析失败") from err
                        break
                else:
                    raise ValueError("HTTP重定向次数过多")
        except (httpx.TimeoutException, TimeoutError) as err:
            raise ValueError("HTTP请求超时") from err
        except httpx.HTTPError as err:
            raise ValueError("HTTP连接失败，请检查管理员配置") from err
        except (LookupError, UnicodeError) as err:
            raise ValueError("HTTP响应编码无效") from err
        if config["type"] == "scrape":
            # HTML parsing/CSS selection can be CPU intensive and runs off the HA loop.
            extracted = await self.hass.async_add_executor_job(_extract, value, config["sensor"])
            scope = dict(variables)
            for sensor, found in zip(config["sensor"], extracted, strict=True):
                if "value_template" in sensor:
                    found = self._value_template(sensor["value_template"], found, scope)
                scope["value"] = found
                if "name" in sensor:
                    name = self._render(sensor["name"], variables, False)
                    if name in PROTECTED:
                        raise ValueError("scrape名称不能覆盖系统保留变量")
                    scope[name] = found
            value = scope["value"]
            variables = scope
        if "value_template" in config:
            value = self._value_template(config["value_template"], value, variables)
        return value


def _extract(html, sensors):
    from bs4 import BeautifulSoup

    data = BeautifulSoup(html, "html.parser")
    result = []
    for sensor in sensors:
        try:
            tag = data.select(sensor["select"])[sensor.get("index", 0)]
            value = (
                tag[sensor["attribute"]]
                if sensor.get("attribute")
                else tag.string
                if tag.name in {"script", "style", "template"}
                else tag.text
            )
        except IndexError, KeyError:
            value = None
        except Exception as err:
            raise ValueError("CSS选择器无效") from err
        result.append(value)
    return result


def _raise(message="SQL模板条件不满足"):
    raise ValueError(str(message)[:200])


def _entity_reference(value):
    import re

    return bool(re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value))


def _period(args, default, max_days):
    now = datetime.now(_BEIJING)

    def parse(value, fallback):
        if value is None:
            return fallback
        if not isinstance(value, str):
            raise ValueError("查询时间必须为ISO日期时间")
        try:
            result = datetime.fromisoformat(value)
            return (
                result.replace(tzinfo=_BEIJING)
                if result.tzinfo is None
                else result.astimezone(_BEIJING)
            )
        except ValueError as err:
            raise ValueError("查询时间无效，请使用北京时间ISO日期时间") from err

    start = parse(args.get("start_time"), now - default)
    end = parse(args.get("end_time"), start + default)
    if start >= end or end - start > timedelta(days=max_days):
        raise ValueError("查询时间范围无效或过大")
    return start, end


def _local_times(value):
    if isinstance(value, datetime):
        return value.astimezone(_BEIJING).isoformat()
    if isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            return parsed.astimezone(_BEIJING).isoformat()
    if isinstance(value, dict):
        return {key: _local_times(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_local_times(item) for item in value]
    return value
