"""复用当前模型并配置默认播报及天气。"""

import probatio as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .api import DOMAIN
from .provider import get_provider


def _schema(hass, defaults):
    sources = [
        {"value": entity.entity_id, "label": hass.states.get(entity.entity_id).name}
        for entity in er.async_get(hass).entities.values()
        if entity.platform == "extended_openai_conversation"
        and entity.domain == "conversation"
        and hass.states.get(entity.entity_id)
    ]
    weather = [s.entity_id for s in hass.states.async_all("weather")]
    return vol.Schema(
        {
            vol.Required(
                "source_entity",
                default=defaults.get("source_entity", sources[0]["value"] if sources else ""),
            ): selector.SelectSelector(selector.SelectSelectorConfig(options=sources)),
            vol.Optional("outputs", default=defaults.get("outputs", [])): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["assist_satellite", "text"], multiple=True)
            ),
            vol.Optional(
                "weather_entity",
                default=defaults.get("weather_entity", weather[0] if weather else ""),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="weather")),
            vol.Optional("location_name", default=defaults.get("location_name", "家")): str,
        }
    )


class AIToolsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                get_provider(self.hass, user_input["source_entity"])
                from types import SimpleNamespace

                from .api import validate_settings
                from .manager import ToolsManager

                temp = SimpleNamespace(data={}, options={})
                check = ToolsManager(self.hass, temp, store=object())
                locations = (
                    {user_input["location_name"]: user_input["weather_entity"]}
                    if user_input.get("weather_entity")
                    else {}
                )
                validate_settings(
                    check, {"outputs": user_input.get("outputs", []), "locations": locations}
                )
            except ValueError, KeyError:
                errors["base"] = "invalid_config"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="AI 家庭助手",
                    data={
                        "source_entity": user_input["source_entity"],
                        "outputs": user_input.get("outputs", []),
                        "locations": locations,
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=_schema(self.hass, user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AIToolsOptionsFlow()


class AIToolsOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        manager = self.config_entry.runtime_data
        defaults = dict(manager.settings)
        if defaults.get("locations"):
            defaults["location_name"], defaults["weather_entity"] = next(
                iter(defaults["locations"].items())
            )
        errors = {}
        if user_input is not None:
            from .api import validate_settings

            try:
                get_provider(self.hass, user_input["source_entity"])
                locations = dict(defaults.get("locations", {}))
                if user_input.get("weather_entity"):
                    locations[user_input.get("location_name", "家")] = user_input["weather_entity"]
                settings = validate_settings(
                    manager,
                    {
                        "source_entity": user_input["source_entity"],
                        "outputs": user_input.get("outputs", []),
                        "locations": locations,
                    },
                )
                await manager.async_settings(settings)
            except ValueError, KeyError:
                errors["base"] = "invalid_config"
            else:
                return self.async_create_entry(title="", data=settings)
        return self.async_show_form(
            step_id="init", data_schema=_schema(self.hass, defaults), errors=errors
        )
