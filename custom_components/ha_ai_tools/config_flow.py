"""独立连接、助手子条目、重新认证和可选旧配置导入。"""

from types import SimpleNamespace
from uuid import uuid4

import probatio as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .api import DOMAIN
from .provider import (
    CONNECTION_DEFAULTS,
    DEFAULT_OPTIONS,
    async_close_providers,
    async_create_client,
    async_import_legacy,
    async_save_connection,
    async_save_profile,
    list_connections,
    validate_connection,
    validate_profile,
)
from .provider import get_provider as get_provider  # compatibility for legacy options callers


def _connection_schema(defaults=None):
    defaults = {**CONNECTION_DEFAULTS, **(defaults or {})}
    fields = {
        vol.Optional("name", default=defaults["name"]): str,
        vol.Required("api_provider", default=defaults["api_provider"]): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    {"value": "openai", "label": "OpenAI 兼容接口（含 DeepSeek、本地模型）"},
                    {"value": "azure", "label": "Azure OpenAI"},
                ]
            )
        ),
        # Credentials are intentionally never a schema default or suggested value.
        vol.Optional("api_key", default=""): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Required("base_url", default=defaults["base_url"]): str,
        vol.Optional("api_version", default=defaults["api_version"]): str,
        vol.Optional("organization", default=defaults["organization"]): str,
        vol.Optional("skip_authentication", default=defaults["skip_authentication"]): bool,
    }
    if defaults.get("api_key_configured") or defaults.get("api_key"):
        fields[vol.Optional("clear_api_key", default=False)] = bool
    return fields


def _business_schema(defaults):
    return {
        vol.Optional("outputs", default=defaults.get("outputs", [])): selector.EntitySelector(
            selector.EntitySelectorConfig(domain=["assist_satellite", "text"], multiple=True)
        ),
        vol.Optional(
            "weather_entity", default=defaults.get("weather_entity", "")
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
        vol.Optional("location_name", default=defaults.get("location_name", "家")): str,
    }


def _profile_schema(entry, defaults=None):
    defaults = {**DEFAULT_OPTIONS, "name": "AI 家庭助手", **(defaults or {})}
    connections = [{"value": c["id"], "label": c["name"]} for c in list_connections(entry)]
    fields = {
        vol.Required("name", default=defaults["name"]): str,
        vol.Required(
            "connection_id",
            default=defaults.get("connection_id", connections[0]["value"] if connections else ""),
        ): selector.SelectSelector(selector.SelectSelectorConfig(options=connections)),
        vol.Required("chat_model", default=defaults["chat_model"]): str,
        vol.Required("prompt", default=defaults["prompt"]): selector.TemplateSelector(),
        vol.Required(
            "functions",
            default=defaults["functions"]
            if isinstance(defaults["functions"], str)
            else _yaml(defaults["functions"]),
        ): selector.TemplateSelector(),
    }
    for key in ("max_tokens", "max_function_calls_per_conversation", "context_threshold"):
        fields[vol.Required(key, default=defaults[key])] = vol.Coerce(int)
    for key in ("top_p", "temperature"):
        fields[vol.Required(key, default=defaults[key])] = vol.Coerce(float)
    for key in ("attach_username", "use_tools"):
        fields[vol.Required(key, default=defaults[key])] = bool
    fields[vol.Required("context_truncate_strategy", default="clear")] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[{"value": "clear", "label": "清除该助手的当前上下文"}]
        )
    )
    return vol.Schema(fields)


def _yaml(value):
    import yaml

    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def _settings(manager, user_input, defaults=None):
    from .api import validate_settings

    locations = dict((defaults or {}).get("locations", {}))
    if user_input.get("weather_entity"):
        locations[user_input.get("location_name", "家")] = user_input["weather_entity"]
    return validate_settings(
        manager, {"outputs": user_input.get("outputs", []), "locations": locations}
    )


def _manager(hass, entry):
    manager = getattr(entry, "runtime_data", None)
    if manager is not None and getattr(manager, "entry", None) is entry:
        return manager, False
    return SimpleNamespace(hass=hass, entry=entry), True


class AIToolsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                connection = validate_connection(user_input)
                identifier = uuid4().hex
                profile = validate_profile(
                    {
                        "connection_id": identifier,
                        "name": user_input.get("name", "AI 家庭助手"),
                        "chat_model": user_input.get("chat_model", DEFAULT_OPTIONS["chat_model"]),
                    }
                )
                from .manager import ToolsManager

                temp = ToolsManager(self.hass, SimpleNamespace(data={}, options={}), store=object())
                settings = _settings(temp, user_input)
                client = await async_create_client(self.hass, connection, validate=True)
                await client.close()
            except ValueError, KeyError:
                errors["base"] = "invalid_config"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input.get("name", "AI 家庭助手"),
                    data=settings,
                    subentries=[
                        {
                            "subentry_type": "connection",
                            "title": connection["name"],
                            "unique_id": f"connection:{identifier}",
                            "data": {**connection, "id": identifier},
                        },
                        {
                            "subentry_type": "conversation",
                            "title": profile["name"],
                            "unique_id": "conversation:default",
                            "data": {**profile, "id": "default"},
                        },
                    ],
                )
        defaults = {**(user_input or {}), "api_key": ""}
        schema = {
            **_connection_schema(defaults),
            vol.Required(
                "chat_model", default=defaults.get("chat_model", DEFAULT_OPTIONS["chat_model"])
            ): str,
            **_business_schema(defaults),
        }
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema), errors=errors)

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry):
        return {"connection": ConnectionSubentryFlow, "conversation": ConversationSubentryFlow}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AIToolsOptionsFlow()

    async def async_step_reauth(self, entry_data=None):
        return await self.async_step_reauth_connection()

    async def async_step_reconfigure(self, user_input=None):
        return await self.async_step_reauth_connection(user_input)

    async def async_step_reauth_connection(self, user_input=None):
        entry = (
            self._get_reauth_entry() if self.source == "reauth" else self._get_reconfigure_entry()
        )
        connections = list_connections(entry)
        if not connections:
            return self.async_abort(reason="no_connection")
        if user_input is not None:
            self._connection_id = user_input["connection_id"]
            return await self.async_step_reauth_credentials()
        if len(connections) == 1:
            self._connection_id = connections[0]["id"]
            return await self.async_step_reauth_credentials()
        return self.async_show_form(
            step_id="reauth_connection",
            data_schema=vol.Schema(
                {
                    vol.Required("connection_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[{"value": c["id"], "label": c["name"]} for c in connections]
                        )
                    )
                }
            ),
        )

    async def async_step_reauth_credentials(self, user_input=None):
        entry = (
            self._get_reauth_entry() if self.source == "reauth" else self._get_reconfigure_entry()
        )
        connection = next(
            (c for c in list_connections(entry) if c["id"] == self._connection_id), None
        )
        if connection is None:
            return self.async_abort(reason="no_connection")
        errors = {}
        if user_input is not None:
            manager, owned = _manager(self.hass, entry)
            try:
                await async_save_connection(manager, {**user_input, "id": self._connection_id})
            except ValueError:
                errors["base"] = "invalid_config"
            else:
                reason = (
                    "reauth_successful" if self.source == "reauth" else "reconfigure_successful"
                )
                return self.async_abort(reason=reason, translation_domain="homeassistant")
            finally:
                if owned:
                    await async_close_providers(manager)
        return self.async_show_form(
            step_id="reauth_credentials",
            data_schema=vol.Schema(_connection_schema(connection)),
            errors=errors,
        )


class AIToolsOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is None:
            return self.async_show_menu(step_id="init", menu_options=["settings", "import_legacy"])
        return await self.async_step_settings(user_input)

    async def async_step_settings(self, user_input=None):
        manager = self.config_entry.runtime_data
        defaults = dict(manager.settings)
        if defaults.get("locations"):
            defaults["location_name"], defaults["weather_entity"] = next(
                iter(defaults["locations"].items())
            )
        errors = {}
        if user_input is not None:
            try:
                settings = _settings(manager, user_input, defaults)
                await manager.async_settings(settings)
            except ValueError, KeyError:
                errors["base"] = "invalid_config"
            else:
                return self.async_create_entry(title="", data=settings)
        return self.async_show_form(
            step_id="settings", data_schema=vol.Schema(_business_schema(defaults)), errors=errors
        )

    async def async_step_import_legacy(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await async_import_legacy(
                    self.config_entry.runtime_data, user_input["source_entity"]
                )
            except ValueError:
                errors["base"] = "invalid_config"
            else:
                return self.async_abort(reason="import_successful")
        return self.async_show_form(
            step_id="import_legacy",
            data_schema=vol.Schema(
                {
                    vol.Required("source_entity"): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="conversation", integration="extended_openai_conversation"
                        )
                    )
                }
            ),
            errors=errors,
        )


class ConnectionSubentryFlow(config_entries.ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        return await self.async_step_init(user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self.async_step_init(user_input)

    async def async_step_init(self, user_input=None):
        entry = self._get_entry()
        existing = self._get_reconfigure_subentry() if self.source == "reconfigure" else None
        defaults = dict(existing.data) if existing else {}
        errors = {}
        if user_input is not None:
            manager, owned = _manager(self.hass, entry)
            try:
                if existing is not None:
                    await async_save_connection(
                        manager, {**user_input, "id": defaults.get("id", existing.subentry_id)}
                    )
                    return self.async_abort(
                        reason="reconfigure_successful", translation_domain="homeassistant"
                    )
                data = validate_connection(user_input)
                client = await async_create_client(self.hass, data, validate=True)
                await client.close()
                identifier = uuid4().hex
                return self.async_create_entry(
                    title=data["name"],
                    data={**data, "id": identifier},
                    unique_id=f"connection:{identifier}",
                )
            except ValueError:
                errors["base"] = "invalid_config"
            finally:
                if owned:
                    await async_close_providers(manager)
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(_connection_schema(defaults)), errors=errors
        )


class ConversationSubentryFlow(config_entries.ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        return await self.async_step_init(user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self.async_step_init(user_input)

    async def async_step_init(self, user_input=None):
        entry = self._get_entry()
        if not list_connections(entry):
            return self.async_abort(reason="no_connection")
        existing = self._get_reconfigure_subentry() if self.source == "reconfigure" else None
        defaults = dict(existing.data) if existing else {}
        errors = {}
        if user_input is not None:
            try:
                if existing is not None:
                    manager, _ = _manager(self.hass, entry)
                    await async_save_profile(
                        manager, {**user_input, "id": defaults.get("id", existing.subentry_id)}
                    )
                    return self.async_abort(
                        reason="reconfigure_successful", translation_domain="homeassistant"
                    )
                data = validate_profile(user_input, entry=entry)
                identifier = (
                    "default"
                    if not any(s.subentry_type == "conversation" for s in entry.subentries.values())
                    else uuid4().hex
                )
                return self.async_create_entry(
                    title=data["name"],
                    data={**data, "id": identifier},
                    unique_id=f"conversation:{identifier}",
                )
            except ValueError:
                errors["base"] = "invalid_config"
        return self.async_show_form(
            step_id="init", data_schema=_profile_schema(entry, defaults), errors=errors
        )
