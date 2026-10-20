import asyncio

import pytest


def test_settings_refuse_empty_key_or_global_bind(tmp_path):
    from ha_ai_tools_speech.config import Settings

    for changes in ({"api_key": ""}, {"bind_host": "0.0.0.0"}):
        with pytest.raises(ValueError):
            Settings(**{"bind_host": "127.0.0.1", "port": 18973, "api_key": "a" * 32,
                        "allowed_sources": ["127.0.0.1"], "models": {}, **changes})


async def test_queue_limit_and_deadline():
    from ha_ai_tools_speech.runtime import InferenceGate, ServiceBusy

    gate = InferenceGate(wait_timeout=0.01)
    async with gate.slot():
        waiting = [asyncio.create_task(gate.run(lambda: asyncio.sleep(0))) for _ in range(4)]
        await asyncio.sleep(0)
        with pytest.raises(ServiceBusy):
            await gate.run(lambda: asyncio.sleep(0))
        results = await asyncio.gather(*waiting, return_exceptions=True)
        assert all(isinstance(r, ServiceBusy) for r in results)
    assert gate.waiting == 0


async def test_disconnect_cancels_inference():
    from ha_ai_tools_speech.runtime import InferenceGate

    gate = InferenceGate()
    active = asyncio.create_task(gate.run(lambda: asyncio.sleep(99)))
    await asyncio.sleep(0)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert await gate.run(lambda: asyncio.sleep(0, result="ready")) == "ready"
