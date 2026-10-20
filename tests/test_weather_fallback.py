from copy import deepcopy
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.ha_ai_tools import models
from tests.test_manager import manager


@pytest.fixture
def weather_house(monkeypatch):
    m = manager()
    states = {
        "weather.home": m.hass.states.get("weather.home"),
        "weather.backup": SimpleNamespace(
            state="rainy",
            attributes={"temperature": 16, "supported_features": 3},
            last_updated=models.now(),
        ),
        "weather.other_city": SimpleNamespace(
            state="sunny", attributes={"temperature": 30}, last_updated=models.now()
        ),
        "zone.home": SimpleNamespace(attributes={"latitude": 30.0, "longitude": 120.0}),
    }
    entries = {
        "primary": SimpleNamespace(data={"track_home": True}, options={}, disabled_by=None),
        "backup": SimpleNamespace(data={"zone": "zone.home"}, options={}, disabled_by=None),
        "other": SimpleNamespace(
            data={"latitude": 40.0, "longitude": 116.0}, options={}, disabled_by=None
        ),
    }
    entities = {
        entity_id: SimpleNamespace(
            entity_id=entity_id, config_entry_id=entry_id, disabled_by=None
        )
        for entity_id, entry_id in (
            ("weather.home", "primary"),
            ("weather.backup", "backup"),
            ("weather.other_city", "other"),
        )
    }
    registry = SimpleNamespace(entities=entities, async_get=entities.get)
    monkeypatch.setattr(er, "async_get", lambda hass: registry)
    m.hass.config = SimpleNamespace(latitude=30.0, longitude=120.0)
    m.hass.config_entries = SimpleNamespace(async_get_entry=entries.get)
    m.hass.states = SimpleNamespace(get=states.get, async_all=lambda: list(states.values()))
    calls = []

    async def service(domain, name, data, **kwargs):
        calls.append((name, data["entity_id"]))
        if name == "get_forecasts":
            return {
                data["entity_id"]: {
                    "forecast": [{"temperature": states[data["entity_id"]].attributes["temperature"]}]
                }
            }

    m.hass.services.async_call = service
    return SimpleNamespace(manager=m, states=states, entries=entries, entities=entities, calls=calls)


def test_sources_match_home_tracking_zone_and_coordinates_but_not_names(weather_house):
    from custom_components.ha_ai_tools.weather import weather_sources

    h = weather_house
    assert weather_sources(h.manager.hass, "weather.home") == ["weather.home", "weather.backup"]
    h.entries["backup"].data = {"latitude": "30.00001", "longitude": "120.00001"}
    assert weather_sources(h.manager.hass, "weather.home") == ["weather.home", "weather.backup"]
    h.entries["backup"].data = {"name": "家", "latitude": 30.1, "longitude": 120.1}
    assert weather_sources(h.manager.hass, "weather.home") == ["weather.home"]


@pytest.mark.parametrize("invalid", [{}, {"zone": "zone.missing"}, {"latitude": "nan", "longitude": 120}, {"latitude": 91, "longitude": 120}])
def test_unverified_location_never_uses_another_city(weather_house, invalid):
    from custom_components.ha_ai_tools.weather import weather_sources

    h = weather_house
    h.entries["primary"].data = invalid
    assert weather_sources(h.manager.hass, "weather.home") == ["weather.home"]


@pytest.mark.parametrize("disabled", ["entity", "entry"])
def test_disabled_weather_sources_are_not_activated(weather_house, disabled):
    from custom_components.ha_ai_tools.weather import weather_sources

    h = weather_house
    (h.entities["weather.backup"] if disabled == "entity" else h.entries["backup"]).disabled_by = "user"
    assert weather_sources(h.manager.hass, "weather.home") == ["weather.home"]


@pytest.mark.parametrize("kind", ["current", "daily", "hourly"])
async def test_offline_primary_uses_healthy_same_place_backup_without_waiting(weather_house, kind):
    h = weather_house
    h.states["weather.home"].state = "unavailable"
    before = deepcopy(h.manager.data)
    value = await h.manager.async_weather("家", kind)
    assert value["entity_id"] == "weather.backup"
    assert value["primary_entity_id"] == "weather.home"
    assert value["fallback_used"] is True
    assert value["temperature"] == 16
    assert value["timezone"] == "Asia/Shanghai"
    assert not any(name == "update_entity" for name, _ in h.calls)
    assert h.manager.data == before
    assert h.manager.settings["locations"]["家"] == "weather.home"


async def test_recovered_primary_is_preferred_on_the_next_query(weather_house):
    h = weather_house
    h.states["weather.home"].state = "unavailable"
    assert (await h.manager.async_weather("家"))["fallback_used"] is True
    h.states["weather.home"].state = "sunny"
    value = await h.manager.async_weather("家")
    assert value["entity_id"] == "weather.home"
    assert value["fallback_used"] is False


@pytest.mark.parametrize("failure", ["network", "timeout", "empty", "unsupported"])
async def test_forecast_failure_or_missing_capability_can_use_backup(weather_house, failure):
    h = weather_house
    original = h.manager.hass.services.async_call
    primary_calls = []

    async def service(domain, name, data, **kwargs):
        if data["entity_id"] == "weather.home":
            primary_calls.append(name)
            if failure == "empty":
                return {"weather.home": {"forecast": []}}
            if failure == "timeout":
                raise TimeoutError("private provider failure")
            raise HomeAssistantError("private provider failure")
        return await original(domain, name, data, **kwargs)

    h.manager.hass.services.async_call = service
    if failure == "unsupported":
        h.states["weather.home"].attributes["supported_features"] = 1
    value = await h.manager.async_weather("家", "hourly")
    assert value["entity_id"] == "weather.backup"
    assert value["forecast"][0]["temperature"] == 16
    assert "private" not in str(value)
    if failure == "unsupported":
        assert primary_calls == []


async def test_total_outage_does_not_return_other_city_or_cached_weather(weather_house):
    h = weather_house
    h.states["weather.home"].state = "unavailable"
    h.states["weather.backup"].state = "unknown"
    with pytest.raises(ValueError, match="同地点.*不可用"):
        await h.manager.async_weather("家")
    assert all(entity != "weather.other_city" for _, entity in h.calls)


async def test_same_place_search_and_requests_have_an_overall_deadline(weather_house, monkeypatch):
    import asyncio

    h = weather_house
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: original_timeout(0.01 if seconds == 20 else seconds))

    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    h.manager.hass.services.async_call = stalled
    with pytest.raises(ValueError, match="超时"):
        await h.manager.async_weather("家", "daily")


async def test_fallback_with_real_ha_registry_states_and_service(tmp_path, monkeypatch):
    from homeassistant.config_entries import ConfigEntries, ConfigEntry
    from homeassistant.core import HomeAssistant, SupportsResponse
    from homeassistant.helpers import area_registry, device_registry

    from custom_components.ha_ai_tools.api import snapshot

    hass = HomeAssistant(str(tmp_path))
    hass.config.latitude, hass.config.longitude = 30.0, 120.0
    hass.config_entries = ConfigEntries(hass, {})
    entries = {
        domain: ConfigEntry(
            domain=domain, data=data, options={}, title=domain, source="user",
            version=1, minor_version=1, unique_id=None, discovery_keys={}, subentries_data=None,
        )
        for domain, data in (("met", {"track_home": True}), ("open_meteo", {"zone": "zone.home"}))
    }
    by_id = {entry.entry_id: entry for entry in entries.values()}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", by_id.get)
    device_registry.async_setup(hass)
    await area_registry.async_load(hass, load_empty=True)
    await device_registry.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    registry = er.async_get(hass)
    primary = registry.async_get_or_create("weather", "met", "primary", config_entry=entries["met"])
    backup = registry.async_get_or_create("weather", "open_meteo", "backup", config_entry=entries["open_meteo"])
    hass.states.async_set("zone.home", "0", {"latitude": 30.0, "longitude": 120.0})
    hass.states.async_set(primary.entity_id, "unavailable", {"supported_features": 3})
    hass.states.async_set(backup.entity_id, "rainy", {"temperature": 16, "supported_features": 3})

    async def forecasts(call):
        assert call.data["entity_id"] == backup.entity_id
        return {backup.entity_id: {"forecast": [{"temperature": 19}]}}

    hass.services.async_register("weather", "get_forecasts", forecasts, supports_response=SupportsResponse.ONLY)
    m = manager()
    m.hass = hass
    m.entry.subentries = {}
    m.entry.options["locations"]["家"] = primary.entity_id
    try:
        value = await m.async_weather("家", "daily")
        assert value["entity_id"] == backup.entity_id
        assert value["fallback_used"] is True
        assert value["forecast"][0]["temperature"] == 19
        page = snapshot(m)
        primary_row = next(row for row in page["weather"] if row["entity_id"] == primary.entity_id)
        assert primary_row["fallback_entities"] == [backup.entity_id]
        assert all("latitude" not in row and "longitude" not in row for row in page["weather"])
    finally:
        await hass.async_stop()
