"""仅按已配置的位置识别可互相替代的天气实体。"""

from math import isclose, isfinite

from homeassistant.helpers import entity_registry as er


def _coordinates(hass, entry):
    if entry is None:
        return None
    data = {**entry.data, **entry.options}
    if data.get("track_home") is True:
        latitude, longitude = hass.config.latitude, hass.config.longitude
    elif "zone" in data:
        zone = hass.states.get(data["zone"]) if isinstance(data["zone"], str) else None
        if zone is None:
            return None
        latitude, longitude = zone.attributes.get("latitude"), zone.attributes.get("longitude")
    else:
        latitude, longitude = data.get("latitude"), data.get("longitude")
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not (isfinite(latitude) and isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


def weather_sources(hass, primary):
    """Keep the primary first; never infer a shared place from entity names."""
    if not getattr(hass, "config_entries", None):
        return [primary]
    registry = er.async_get(hass)
    entity = registry.async_get(primary)
    entry = hass.config_entries.async_get_entry(entity.config_entry_id) if entity else None
    origin = _coordinates(hass, entry)
    if origin is None:
        return [primary]
    sources = [primary]
    for entity in sorted(registry.entities.values(), key=lambda item: item.entity_id):
        if entity.entity_id == primary or not entity.entity_id.startswith("weather.") or entity.disabled_by:
            continue
        entry = hass.config_entries.async_get_entry(entity.config_entry_id) if entity.config_entry_id else None
        if entry is None or entry.disabled_by:
            continue
        coordinates = _coordinates(hass, entry)
        # 只容许坐标舍入误差，纬度最多约 11 米。
        if coordinates and all(isclose(a, b, rel_tol=0, abs_tol=0.0001) for a, b in zip(origin, coordinates, strict=True)):
            sources.append(entity.entity_id)
    return sources
