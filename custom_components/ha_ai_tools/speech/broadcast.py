"""提醒按目标选择音色，调用前统一验证测试目标。"""

import asyncio
import json
import logging

_LOGGER = logging.getLogger(__name__)


def make_media(manager, message, options):
    from homeassistant.components.tts import generate_media_source_id

    return generate_media_source_id(manager.hass, message, engine=options["tts_entity_id"], language=options.get("language", "zh"),
                                    options={"voice": options["voice"], "speed": options.get("speed", 1.0)}, cache=False)


async def async_send(manager, message, outputs, *, test_id=None):
    runtime = getattr(manager, "speech", None)
    if test_id is not None:
        if runtime is None or not getattr(runtime, "testing", None):
            raise ValueError("测试身份无法验证，未进行播报")
        outputs = runtime.testing.validate_targets(test_id, outputs)
    store = getattr(runtime, "store", None)
    voices = store.data["device_voices"] if store else {}
    media = {}

    async def send(entity_id):
        try:
            state = manager.hass.states.get(entity_id)
            if state is None or state.state in ("unavailable", "unknown"):
                return entity_id, "failed"
            if entity_id.startswith("assist_satellite."):
                options = voices.get(entity_id)
                if options:
                    key = json.dumps(options, sort_keys=True)
                    if key not in media:
                        media[key] = make_media(manager, message, options)
                    data = {"entity_id": entity_id, "media_id": media[key]}
                else:
                    data = {"entity_id": entity_id, "message": message}
                await manager.hass.services.async_call("assist_satellite", "announce", data, blocking=True)
            elif entity_id.startswith("text."):
                for start in range(0, len(message), 200):
                    await manager.hass.services.async_call("text", "set_value", {"entity_id": entity_id, "value": message[start:start + 200]}, blocking=True)
            else:
                return entity_id, "failed"
            return entity_id, "delivered"
        except Exception as error:
            _LOGGER.warning("播报失败: %s", type(error).__name__)
            return entity_id, "failed"

    return dict(await asyncio.gather(*(send(e) for e in dict.fromkeys(outputs))))
