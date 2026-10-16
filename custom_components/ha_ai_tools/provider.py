"""独立模型连接与助手；凭据仅写入 HA 配置子条目。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from copy import deepcopy
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

DEFAULT_OPTIONS = {
    "prompt": "你是 {{ ha_name }} 的家庭助手。使用提供的设备状态与工具真实结果，简短地用中文回答。当前北京时间：{{ now() }}。",
    "chat_model": "gpt-4o-mini",
    "max_tokens": 2000,
    "top_p": 1.0,
    "temperature": 0.5,
    "max_function_calls_per_conversation": 8,
    "functions": "[]",
    "attach_username": False,
    "use_tools": True,
    "context_threshold": 13000,
    "context_truncate_strategy": "clear",
}
CONNECTION_DEFAULTS = {
    "name": "模型连接",
    "api_provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_version": "",
    "organization": "",
    "skip_authentication": False,
}
CONNECTION_FIELDS = {*CONNECTION_DEFAULTS, "api_key"}
PROFILE_FIELDS = {*DEFAULT_OPTIONS, "name", "connection_id"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _subentries(entry, kind):
    return [s for s in getattr(entry, "subentries", {}).values() if s.subentry_type == kind]


def _identity(subentry):
    return subentry.data.get("id", subentry.subentry_id)


def _find(entry, kind, identifier):
    return next((s for s in _subentries(entry, kind) if _identity(s) == identifier), None)


def _new_id(value=None):
    value = value or uuid4().hex
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("配置 ID 无效")
    return value


def list_connections(entry):
    """Return only the connection metadata suitable for the browser."""
    result = []
    for subentry in _subentries(entry, "connection"):
        data = subentry.data
        result.append(
            {
                "id": _identity(subentry),
                "subentry_id": subentry.subentry_id,
                **{
                    key: deepcopy(data.get(key, default))
                    for key, default in CONNECTION_DEFAULTS.items()
                },
                "api_key_configured": bool(data.get("api_key")),
            }
        )
    return result


def list_profiles(entry):
    result = []
    for subentry in _subentries(entry, "conversation"):
        data = subentry.data
        result.append(
            {
                **deepcopy(DEFAULT_OPTIONS),
                **{key: deepcopy(data[key]) for key in PROFILE_FIELDS if key in data},
                "id": _identity(subentry),
                "name": data.get("name", subentry.title),
                "subentry_id": subentry.subentry_id,
            }
        )
    # Lightweight tests and old entries have no subentries. Never borrow an EOC client.
    if not result and not hasattr(entry, "subentries"):
        options = getattr(entry, "options", {})
        result.append(
            {
                **deepcopy(DEFAULT_OPTIONS),
                **{key: deepcopy(options[key]) for key in DEFAULT_OPTIONS if key in options},
                "id": "default",
                "name": "AI 家庭助手",
                "connection_id": "",
            }
        )
    return result


def get_profile(entry, profile_id=None):
    identifier = profile_id or "default"
    for profile in list_profiles(entry):
        if profile["id"] == identifier:
            return profile
    raise ValueError("对话助手不存在，请检查助手配置")


def validate_connection(payload, previous=None):
    """Merge an edit without echoing secrets or silently clearing a key."""
    previous = previous or {}
    result = {**CONNECTION_DEFAULTS, **{k: previous[k] for k in CONNECTION_FIELDS if k in previous}}
    result.update(
        {key: payload[key] for key in CONNECTION_FIELDS if key in payload and key != "api_key"}
    )
    if payload.get("clear_api_key"):
        result["api_key"] = ""
    elif payload.get("api_key"):
        result["api_key"] = payload["api_key"]
    else:
        result.setdefault("api_key", "")
    for key in ("name", "api_provider", "base_url", "api_version", "organization", "api_key"):
        if not isinstance(result[key], str):
            raise ValueError("连接参数必须是文本")
    result["name"] = result["name"].strip() or "模型连接"
    result["base_url"] = result["base_url"].strip().rstrip("/")
    if result["api_provider"] not in {"openai", "azure"}:
        raise ValueError("请选择 OpenAI 兼容接口或 Azure")
    if not isinstance(result["skip_authentication"], bool):
        raise ValueError("跳过认证必须为开关值")
    parsed = urlsplit(result["base_url"])
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Base URL 必须为不含凭据的 HTTP 或 HTTPS 地址")
    if result["api_provider"] == "azure" and not result["api_version"]:
        raise ValueError("Azure 连接需要 API Version")
    if not result["skip_authentication"] and not result["api_key"]:
        raise ValueError("请填写 API 密钥，或明确启用跳过认证")
    return result


def validate_profile(payload, previous=None, *, entry=None):
    from homeassistant.helpers.template import TemplateEnvironment
    from jinja2 import TemplateSyntaxError

    result = {**deepcopy(DEFAULT_OPTIONS), "name": "AI 家庭助手", "connection_id": ""}
    result.update({k: deepcopy(v) for k, v in (previous or {}).items() if k in PROFILE_FIELDS})
    result.update({k: deepcopy(v) for k, v in payload.items() if k in PROFILE_FIELDS})
    for key in ("name", "connection_id", "chat_model", "prompt", "context_truncate_strategy"):
        if not isinstance(result[key], str):
            raise ValueError("助手参数必须为文本")
    if not result["name"].strip() or not result["chat_model"].strip():
        raise ValueError("助手名称和模型不能为空")
    for key, minimum, maximum in (
        ("max_tokens", 1, 1000000),
        ("max_function_calls_per_conversation", 0, 1000000),
        ("context_threshold", 1, 10000000),
    ):
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise ValueError(f"{key} 超出允许范围")
    for key, maximum in (("top_p", 1), ("temperature", 2)):
        if (
            isinstance(result[key], bool)
            or not isinstance(result[key], (int, float))
            or not 0 <= result[key] <= maximum
        ):
            raise ValueError(f"{key} 超出允许范围")
    for key in ("attach_username", "use_tools"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} 必须为开关值")
    if result["context_truncate_strategy"] != "clear":
        raise ValueError("上下文截断策略仅支持 clear")
    try:
        TemplateEnvironment(None).parse(result["prompt"])
    except TemplateSyntaxError:
        raise ValueError("提示词模板无效") from None
    from .functions import compile_functions
    from .tools import TOOLS

    reserved = [tool["function"]["name"] for tool in TOOLS]
    compile_functions(result["functions"], reserved=reserved)
    if entry is not None and _find(entry, "connection", result["connection_id"]) is None:
        raise ValueError("请选择存在的模型连接")
    return result


class _NoAuthentication(httpx.Auth):
    """Explicitly disable both OpenAI and Azure credentials."""

    def auth_flow(self, request):
        request.headers.pop("authorization", None)
        request.headers.pop("api-key", None)
        yield request


def _http_client(skip_authentication):
    from homeassistant.util.ssl import SSL_ALPN_HTTP11, client_context

    # Own this client's lifecycle; closing a connection must not close HA's shared client.
    return httpx.AsyncClient(
        verify=client_context(alpn_protocols=SSL_ALPN_HTTP11),
        auth=_NoAuthentication() if skip_authentication else None,
    )


async def async_create_client(hass, data, *, validate=False):
    """Create an owned SDK client; errors deliberately exclude response bodies and secrets."""
    from openai import (
        APIConnectionError,
        AsyncAzureOpenAI,
        AsyncOpenAI,
        AuthenticationError,
        OpenAIError,
    )

    skip = data.get("skip_authentication", False)
    executor = getattr(hass, "async_add_executor_job", None)
    http_client = (
        await executor(_http_client, skip)
        if executor is not None
        else await asyncio.to_thread(_http_client, skip)
    )
    kwargs = {
        "api_key": data.get("api_key") or "no-authentication",
        "organization": data.get("organization") or None,
        "http_client": http_client,
        "timeout": 30.0,
        "max_retries": 0,
    }
    client = None
    try:
        if data.get("api_provider") == "azure":
            client = AsyncAzureOpenAI(
                azure_endpoint=data["base_url"], api_version=data["api_version"], **kwargs
            )
        else:
            client = AsyncOpenAI(base_url=data.get("base_url") or None, **kwargs)
        if validate and not skip:
            await client.models.list(timeout=10.0)
        return client
    except AuthenticationError:
        error = "模型连接认证失败，请检查 API 密钥"
    except APIConnectionError:
        error = "无法连接模型服务，请检查地址与网络"
    except OpenAIError, ValueError, httpx.HTTPError:
        error = "模型连接验证失败，请检查提供方、API 版本与地址"
    if client is not None:
        await client.close()
    else:
        await kwargs["http_client"].aclose()
    raise ValueError(error) from None


def _cache(manager):
    if not hasattr(manager, "_provider_clients"):
        manager._provider_clients = {}
    return manager._provider_clients


def _lock(manager):
    if not hasattr(manager, "_provider_lock"):
        manager._provider_lock = asyncio.Lock()
    return manager._provider_lock


async def async_get_provider(manager, profile_id=None):
    async with _lock(manager):
        profile = get_profile(manager.entry, profile_id)
        subentry = _find(manager.entry, "connection", profile.get("connection_id"))
        if subentry is None:
            raise ValueError("请先配置独立模型连接，或导入现有 EOC 配置")
        identifier = _identity(subentry)
        data = dict(subentry.data)
        cached = _cache(manager).get(identifier)
        if cached is None or cached[0] != data:
            client = await async_create_client(manager.hass, data)
            _cache(manager)[identifier] = (data, client)
            if cached is not None:
                await cached[1].close()
        return _cache(manager)[identifier][1], profile


async def async_test_connection(manager, connection_id=None, profile_id=None):
    """Explicit connection test, including anonymous endpoints, without caching credentials."""
    from openai import APIConnectionError, AuthenticationError, OpenAIError

    async with _lock(manager):
        identifier = connection_id or get_profile(manager.entry, profile_id).get("connection_id")
        subentry = _find(manager.entry, "connection", identifier)
        if subentry is None:
            raise ValueError("模型连接不存在")
        data = dict(subentry.data)
    client = await async_create_client(manager.hass, data)
    try:
        await client.models.list(timeout=10.0)
    except AuthenticationError:
        raise ValueError("模型连接认证失败，请检查 API 密钥") from None
    except APIConnectionError:
        raise ValueError("无法连接模型服务，请检查地址与网络") from None
    except OpenAIError, ValueError, httpx.HTTPError:
        raise ValueError("模型连接验证失败，请检查提供方、API 版本与地址") from None
    finally:
        await client.close()
    return {"id": identifier, "connected": True}


def _save_subentry(manager, kind, identifier, data):
    from homeassistant.config_entries import ConfigSubentry

    existing = _find(manager.entry, kind, identifier)
    data = {**data, "id": identifier}
    if existing is not None:
        manager.hass.config_entries.async_update_subentry(
            manager.entry, existing, data=data, title=data["name"]
        )
    else:
        manager.hass.config_entries.async_add_subentry(
            manager.entry,
            ConfigSubentry(
                subentry_id=uuid4().hex,
                subentry_type=kind,
                title=data["name"],
                data=MappingProxyType(data),
                unique_id=f"{kind}:{identifier}",
            ),
        )


async def async_save_connection(manager, payload):
    async with _lock(manager):
        identifier = _new_id(payload.get("id"))
        existing = _find(manager.entry, "connection", identifier)
        if payload.get("delete"):
            if existing is None:
                raise ValueError("模型连接不存在")
            if any(p.get("connection_id") == identifier for p in list_profiles(manager.entry)):
                raise ValueError("该连接仍被对话助手引用，请先修改助手")
            manager.hass.config_entries.async_remove_subentry(manager.entry, existing.subentry_id)
            old = _cache(manager).pop(identifier, None)
            if old is not None:
                await old[1].close()
            return {"id": identifier, "deleted": True}
        data = validate_connection(payload, dict(existing.data) if existing else None)
        client = await async_create_client(manager.hass, data, validate=True)
        try:
            _save_subentry(manager, "connection", identifier, data)
        except BaseException:
            await client.close()
            raise
        old = _cache(manager).get(identifier)
        _cache(manager)[identifier] = ({**data, "id": identifier}, client)
        if old is not None:
            await old[1].close()
        return {"id": identifier, "saved": True}


async def async_save_profile(manager, payload):
    async with _lock(manager):
        identifier = _new_id(
            payload.get("id")
            or ("default" if not _subentries(manager.entry, "conversation") else None)
        )
        existing = _find(manager.entry, "conversation", identifier)
        if payload.get("delete"):
            if identifier == "default":
                raise ValueError("默认助手不能删除，可以修改其配置")
            if existing is None:
                raise ValueError("对话助手不存在")
            manager.hass.config_entries.async_remove_subentry(manager.entry, existing.subentry_id)
            return {"id": identifier, "deleted": True}
        data = validate_profile(
            payload, dict(existing.data) if existing else None, entry=manager.entry
        )
        _save_subentry(manager, "conversation", identifier, data)
        return {"id": identifier, "saved": True}


def get_provider(hass, source_entity):
    """Legacy configuration inspection only; never return or inspect EOC runtime_data."""
    from homeassistant.helpers import entity_registry as er

    entity = er.async_get(hass).async_get(source_entity)
    if entity is None or not entity.config_entry_id:
        raise ValueError("请选择已配置的 Extended OpenAI 对话来源")
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    if entry is None or entry.domain != "extended_openai_conversation":
        raise ValueError("所选来源不是 Extended OpenAI Conversation")
    return entry, entity.config_subentry_id


def _legacy_id(*values):
    return "legacy_" + hashlib.sha256(":".join(values).encode()).hexdigest()[:20]


async def async_import_legacy(manager, source_entity):
    """Validate every candidate first, then atomically activate owned configuration."""
    async with _lock(manager):
        legacy, selected = get_provider(manager.hass, source_entity)
        connection_id = _legacy_id(legacy.entry_id)
        existing = _find(manager.entry, "connection", connection_id)
        if existing is not None:
            return {
                "id": connection_id,
                "saved": True,
                "already_imported": True,
                "profiles": [
                    p["id"]
                    for p in list_profiles(manager.entry)
                    if p.get("connection_id") == connection_id
                ],
            }
        data = validate_connection({**dict(legacy.data), "name": legacy.title})
        conversations = _subentries(legacy, "conversation")
        if selected is not None:
            conversations.sort(key=lambda s: s.subentry_id != selected)
            if not any(s.subentry_id == selected for s in conversations):
                raise ValueError("旧对话助手不存在，请重新选择来源")
        candidates = []
        for index, subentry in enumerate(conversations or [None]):
            raw = dict(subentry.data) if subentry is not None else dict(legacy.options)
            # Older EOC entries used options before conversation subentries existed.
            profile = validate_profile(
                {
                    **raw,
                    "connection_id": connection_id,
                    "name": subentry.title if subentry is not None else legacy.title,
                }
            )
            identifier = (
                "default"
                if index == 0 and not _subentries(manager.entry, "conversation")
                else _legacy_id(legacy.entry_id, subentry.subentry_id if subentry else "default")
            )
            candidates.append((identifier, profile))
        client = await async_create_client(manager.hass, data, validate=True)
        before = set(getattr(manager.entry, "subentries", {}))
        try:
            _save_subentry(manager, "connection", connection_id, data)
            for identifier, profile in candidates:
                _save_subentry(manager, "conversation", identifier, profile)
        except BaseException:
            # No existing subentry is overwritten by migration; rollback candidates only.
            for identifier in set(getattr(manager.entry, "subentries", {})) - before:
                manager.hass.config_entries.async_remove_subentry(manager.entry, identifier)
            await client.close()
            raise
        _cache(manager)[connection_id] = ({**data, "id": connection_id}, client)
        return {
            "id": connection_id,
            "saved": True,
            "profiles": [identifier for identifier, _ in candidates],
            "warnings": [
                "已保留原提示词和函数；独立助手固定遵守北京时间、真实工具结果与记忆归属约束。",
                "引用 extended_openai_conversation 服务或事件的自动化需要逐项迁移。",
            ],
        }


async def async_close_providers(manager):
    async with _lock(manager):
        clients = list(_cache(manager).values())
        _cache(manager).clear()
        await asyncio.gather(*(client.close() for _, client in clients), return_exceptions=True)
