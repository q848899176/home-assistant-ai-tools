"""EOC 2.0.3 compatible function configuration and execution.

See NOTICE for the upstream MIT attribution. This package has no EOC runtime dependency.
"""

import copy
import json
import re
from urllib.parse import urlsplit

import yaml
from homeassistant.helpers.template import Template, TemplateEnvironment
from jinja2 import TemplateSyntaxError
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry

TYPES = {"native", "script", "template", "rest", "scrape", "composite", "sqlite"}
NATIVE_NAMES = {
    "execute_service",
    "execute_service_single",
    "add_automation",
    "get_history",
    "get_energy",
    "get_statistics",
    "get_user_from_user_id",
}
PROTECTED = {
    "is_exposed",
    "is_exposed_entity_in_query",
    "exposed_entities",
    "raise",
    "hass",
    "states",
    "user_input",
    "current_device_id",
    "ha_name",
    "_function_result",
}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _bounded(value, depth=0, ancestors=None, count=None):
    """Reject cycles before deepcopy/YAML processing can recurse indefinitely."""
    ancestors = set() if ancestors is None else ancestors
    count = [0] if count is None else count
    count[0] += 1
    if depth > 24 or count[0] > 15000:
        raise ValueError("函数配置结构过深或过大")
    if isinstance(value, (dict, list)):
        if id(value) in ancestors:
            raise ValueError("函数配置不能循环引用")
        ancestors.add(id(value))
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            _bounded(child, depth + 1, ancestors, count)
        ancestors.remove(id(value))


def _template(value):
    if not isinstance(value, (str, Template)):
        raise ValueError("模板必须是字符串")
    try:
        TemplateEnvironment(None).parse(value.template if isinstance(value, Template) else value)
    except TemplateSyntaxError as err:
        raise ValueError("函数模板语法错误") from err


def origin(value):
    """An origin must be fixed by administrator configuration, never model input."""
    if not isinstance(value, str):
        raise ValueError("HTTP地址必须是字符串")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or any(x in parts.netloc for x in "{}\\")
        ):
            raise ValueError
        return (
            parts.scheme,
            parts.hostname.lower(),
            parts.port or (443 if parts.scheme == "https" else 80),
        )
    except ValueError as err:
        raise ValueError("HTTP地址必须使用管理员配置的固定主机范围") from err


def allowed_origins(config):
    values = []
    if config.get("resource"):
        values.append(config["resource"])
    elif config.get("resource_template"):
        value = config["resource_template"]
        value = value.template if isinstance(value, Template) else value
        try:
            origin(value)
        except ValueError:
            pass
        else:
            values.append(value)
    allowed = config.get("allowed_hosts", [])
    if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
        raise ValueError("allowed_hosts必须是固定HTTP origin列表")
    values.extend(allowed)
    if not values:
        raise ValueError("资源模板需要管理员配置固定主机范围")
    return {origin(value) for value in values}


def _function(config, depth=0):
    if not isinstance(config, dict) or config.get("type") not in TYPES:
        raise ValueError("函数执行器类型无效")
    if depth > 12:
        raise ValueError("组合函数嵌套过深")
    kind = config["type"]
    variable = config.get("response_variable")
    if variable is not None and (
        not isinstance(variable, str) or not _VARIABLE.fullmatch(variable) or variable in PROTECTED
    ):
        raise ValueError("响应变量无效或使用保留名称")
    if kind == "native":
        if config.get("name") not in NATIVE_NAMES:
            raise ValueError("未知原生函数")
    elif kind == "template":
        _template(config.get("value_template"))
        if "parse_result" in config and not isinstance(config["parse_result"], bool):
            raise ValueError("parse_result必须是布尔值")
    elif kind in {"script", "composite"}:
        sequence = config.get("sequence")
        if not isinstance(sequence, list) or not sequence or len(sequence) > 100:
            raise ValueError("函数sequence必须是非空列表且不超过100步")
        if kind == "composite":
            for child in sequence:
                _function(child, depth + 1)
        else:
            _script_structure(sequence)
    elif kind in {"rest", "scrape"}:
        if not config.get("resource") and not config.get("resource_template"):
            raise ValueError("HTTP函数需要resource或resource_template")
        allowed_origins(config)
        for key in ("resource_template", "payload_template", "value_template"):
            if key in config:
                _template(config[key])
        if config.get("method", "GET").upper() not in {
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
        }:
            raise ValueError("HTTP方法无效")
        timeout = config.get("timeout", 10)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 120
        ):
            raise ValueError("HTTP超时必须在0至120秒内")
        if not isinstance(config.get("headers", {}), dict):
            raise ValueError("HTTP headers必须是对象")
        if kind == "scrape":
            sensors = config.get("sensor")
            if not isinstance(sensors, list) or not sensors or len(sensors) > 100:
                raise ValueError("scrape需要非空sensor列表")
            for sensor in sensors:
                if not isinstance(sensor, dict) or not isinstance(sensor.get("select"), str):
                    raise ValueError("scrape sensor缺少CSS选择器")
                if not isinstance(sensor.get("index", 0), int) or isinstance(
                    sensor.get("index", 0), bool
                ):
                    raise ValueError("scrape index必须是整数")
                if "value_template" in sensor:
                    _template(sensor["value_template"])
                if "name" in sensor:
                    _template(sensor["name"])
    else:
        path = config.get("db_url", "")
        if not isinstance(path, str) or any(x in path for x in ("{{", "{%")):
            raise ValueError("数据库路径必须由管理员固定配置")
        _template(config.get("query", "{{ query }}"))
        if "single" in config and not isinstance(config["single"], bool):
            raise ValueError("single必须是布尔值")
        for key, maximum in (("timeout", 10), ("max_rows", 2000)):
            value = config.get(key, 2 if key == "timeout" else 1000)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= maximum
            ):
                raise ValueError(f"sqlite {key}超出安全范围")


def _script_structure(sequence):
    """Check syntax without a live HA context; HA validates full actions at execution."""
    from homeassistant.helpers import config_validation as cv

    def templates(value):
        if isinstance(value, (str, Template)):
            _template(value)
        elif isinstance(value, dict):
            for item in value.values():
                templates(item)
        elif isinstance(value, list):
            for item in value:
                templates(item)

    for step in sequence:
        if not isinstance(step, dict):
            raise ValueError("HA脚本每步必须是对象")
        try:
            cv.determine_script_action(step)
        except Exception as err:
            raise ValueError("HA脚本操作类型无效") from err
        templates(step)
        for key in ("variables", "data", "data_template", "target"):
            if key in step and not isinstance(step[key], dict):
                raise ValueError(f"HA脚本{key}必须是对象")
        for key in ("action", "service"):
            if key in step and not isinstance(step[key], (str, Template)):
                raise ValueError("HA脚本服务必须是字符串")
        for key in ("sequence", "then", "else", "default", "parallel"):
            if key in step:
                if not isinstance(step[key], list):
                    raise ValueError("HA脚本子序列必须是列表")
                _script_structure(step[key])
        if "repeat" in step:
            if not isinstance(step["repeat"], dict) or not isinstance(
                step["repeat"].get("sequence"), list
            ):
                raise ValueError("HA脚本repeat缺少sequence")
            _script_structure(step["repeat"]["sequence"])
        if "choose" in step:
            if not isinstance(step["choose"], list):
                raise ValueError("HA脚本choose必须是列表")
            for option in step["choose"]:
                if not isinstance(option, dict) or not isinstance(option.get("sequence"), list):
                    raise ValueError("HA脚本choose缺少sequence")
                _script_structure(option["sequence"])


def _schema(schema):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("函数parameters必须是object JSON Schema")
    _bounded(schema)

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value and not str(value["$ref"]).startswith("#"):
                raise ValueError("参数Schema不能引用外部资源")
            if any(key in value for key in ("$dynamicRef", "$recursiveRef")):
                raise ValueError("参数Schema不能使用动态外部引用")
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    if PROTECTED.intersection(schema.get("properties", {})):
        raise ValueError("参数不能覆盖系统保留变量")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as err:
        raise ValueError("函数参数JSON Schema无效") from err


def compile_functions(raw, reserved=()):
    """Validate EOC spec/function YAML and return independent plain configurations."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        if len(raw) > 128000:
            raise ValueError("函数YAML过大")
        try:
            raw = yaml.safe_load(raw)
        except yaml.YAMLError as err:
            raise ValueError("函数YAML语法错误") from err
    _bounded(raw)
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("函数配置必须是列表且最多100个")
    result, names = [], set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"spec", "function"}:
            raise ValueError("每个函数必须包含spec和function")
        spec, config = item["spec"], item["function"]
        if (
            not isinstance(spec, dict)
            or not isinstance(spec.get("name"), str)
            or not _NAME.fullmatch(spec["name"])
        ):
            raise ValueError("函数名称格式无效")
        name = spec["name"]
        if (
            name in names
            or name in reserved
            and not (
                name == "execute_services"
                and isinstance(config, dict)
                and config.get("type") == "native"
                and config.get("name") == "execute_service"
            )
        ):
            raise ValueError(f"函数名称重名或使用保留名称：{name}")
        _schema(spec.get("parameters", {"type": "object", "properties": {}}))
        _function(config)
        names.add(name)
        result.append(copy.deepcopy(item))
    return result


def validate_call(spec, args):
    """Validate model JSON before any executor can cause a side effect."""
    if not isinstance(args, dict) or PROTECTED.intersection(args):
        raise ValueError("函数参数必须是对象且不能覆盖系统保留变量")
    _bounded(args)
    try:
        if len(json.dumps(args, ensure_ascii=False, allow_nan=False)) > 64000:
            raise ValueError("函数参数过大")
        schema = spec.get("parameters", {"type": "object", "properties": {}})
        _schema(schema)
        Draft202012Validator(schema, registry=Registry()).validate(args)
    except ValidationError as err:
        location = ".".join(str(part) for part in err.absolute_path)
        raise ValueError(f"函数参数不符合Schema：{location or 'arguments'}") from err
    except (TypeError, RecursionError) as err:
        raise ValueError("函数参数必须是有效JSON") from err


from .executor import FunctionExecutor  # noqa: E402

__all__ = ["FunctionExecutor", "compile_functions", "validate_call"]
