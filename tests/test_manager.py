import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.ha_ai_tools import models
from custom_components.ha_ai_tools.manager import ToolsManager


async def test_repeated_reminder_after_restart_jumps_to_future():
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder(
        {"message": "喝水", "in_minutes": 60, "repeat": "daily"}, "repeat"
    )
    m.data["reminders"][0]["at"] = (models.now() - timedelta(minutes=1)).isoformat()
    restarted = manager(m.data)
    await restarted.async_load()
    assert restarted.data["reminders"][0]["id"] == r["id"]
    assert restarted.data["reminders"][0]["at"] > models.now().isoformat()


async def test_old_memory_helper_is_imported_once_without_secrets():
    m = manager()
    original = m.hass.states.get
    m.hass.states.get = lambda eid: (
        SimpleNamespace(state="我叫小明，喜欢简短回答")
        if eid == "input_text.jarvis_long_memory"
        else original(eid)
    )
    await m.async_load()
    assert m.data["memories"][0]["text"] == "我叫小明，喜欢简短回答"
    assert m.data["legacy_imported"]


@pytest.mark.parametrize("operation", ["cancel", "remember", "settings", "forget"])
async def test_failed_save_does_not_change_live_state(operation):
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "save")
    memory = await m.async_remember(
        {"key": "style", "text": "喜欢简短回答", "evidence": "我喜欢简短回答"},
        "我喜欢简短回答",
        "family",
    )
    before = deepcopy(m.data)

    async def fail(data):
        raise OSError("synthetic disk failure")

    m.store.async_save = fail
    with pytest.raises(OSError):
        if operation == "cancel":
            await m.async_cancel_reminder(r["id"])
        elif operation == "remember":
            await m.async_remember(
                {"key": "temp", "text": "喜欢26度", "evidence": "我喜欢26度"},
                "我喜欢26度",
                "family",
            )
        elif operation == "forget":
            await m.async_forget(memory["id"])
        else:
            from custom_components.ha_ai_tools.api import async_action

            await async_action(m, "save_settings", {"outputs": []})
    assert m.data == before


async def test_pending_automation_reuses_id_across_turns_but_completed_does_not():
    m = manager()
    await m.async_load()
    first = await m.async_prepare_automation("same-yaml", "turn1")
    second = await m.async_prepare_automation("same-yaml", "turn2")
    assert first == second
    await m.async_finish_automation(first, True)
    third = await m.async_prepare_automation("same-yaml", "turn3")
    assert third != first


class Store:
    def __init__(self, data=None):
        self.data = deepcopy(data)

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        self.data = deepcopy(data)


class Services:
    def __init__(self):
        self.active = 0
        self.peak = 0

    def has_service(self, domain, service):
        return True

    async def async_call(self, domain, service, data, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            if data.get("entity_id") == "text.failed_play_text":
                raise ValueError("offline")
            return {"weather.home": {"forecast": [{"temperature": 20, "condition": "sunny"}]}}
        finally:
            self.active -= 1


def manager(data=None):
    states = {
        "text.speaker_play_text": SimpleNamespace(
            state="", attributes={"friendly_name": "音箱 播放文本"}
        ),
        "text.failed_play_text": SimpleNamespace(
            state="", attributes={"friendly_name": "离线音箱 播放文本"}
        ),
        "assist_satellite.voice": SimpleNamespace(
            state="idle", attributes={"friendly_name": "语音终端", "supported_features": 1}
        ),
        "weather.home": SimpleNamespace(
            state="sunny",
            attributes={"temperature": 18, "supported_features": 3},
            last_updated=models.now(),
        ),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get), services=Services())
    entry = SimpleNamespace(
        entry_id="test",
        options={"outputs": ["text.speaker_play_text"], "locations": {"家": "weather.home"}},
    )
    return ToolsManager(hass, entry, store=Store(data))


async def test_reminder_survives_new_manager_and_can_be_cancelled():
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "交电费", "in_minutes": 60}, "request-a")
    restored = manager(m.store.data)
    await restored.async_load()
    assert restored.data["reminders"][0]["id"] == r["id"]
    await restored.async_cancel_reminder(r["id"])
    assert restored.store.data["reminders"][0]["status"] == "cancelled"


async def test_reminder_retried_with_same_request_is_not_duplicated():
    m = manager()
    await m.async_load()
    a = await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "same")
    b = await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "same")
    assert a["id"] == b["id"]
    assert len(m.data["reminders"]) == 1


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed", "missed"])
async def test_history_record_can_be_deleted_and_stays_deleted_after_reload(status):
    m = manager()
    await m.async_load()
    history = await m.async_add_reminder({"message": "历史记录", "in_minutes": 60}, "history")
    pending = await m.async_add_reminder({"message": "洗头", "in_minutes": 120}, "pending")
    m.data["reminders"][0]["status"] = status
    result = await m.async_delete_reminder_history(history["id"])
    assert result == {"id": history["id"], "deleted": 1}
    restored = manager(m.store.data)
    await restored.async_load()
    assert restored.data["reminders"] == [pending]


@pytest.mark.parametrize("status", ["pending", "delivering", "unknown_future_status"])
async def test_active_or_unknown_reminder_cannot_be_deleted_as_history(status):
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "洗头", "in_minutes": 60}, "active")
    m.data["reminders"][0]["status"] = status
    before = deepcopy(m.data)
    with pytest.raises(ValueError):
        await m.async_delete_reminder_history(r["id"])
    assert m.data == before


@pytest.mark.parametrize("reminder_id", [None, "", "missing"])
async def test_missing_history_id_cannot_clear_other_records(reminder_id):
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "历史记录", "in_minutes": 60}, "history")
    await m.async_cancel_reminder(r["id"])
    with pytest.raises(ValueError):
        await m.async_delete_reminder_history(reminder_id)
    assert len(m.data["reminders"]) == 1


async def test_bulk_history_clear_preserves_pending_and_delivering_reminders():
    m = manager()
    await m.async_load()
    statuses = ["pending", "delivering", "completed", "cancelled", "failed", "missed", "new"]
    for status in statuses:
        await m.async_add_reminder({"message": status, "in_minutes": 60}, status)
        m.data["reminders"][-1]["status"] = status
    before = deepcopy(m.data["reminders"])
    result = await m.async_clear_reminder_history()
    assert result == {"deleted": 4}
    assert m.data["reminders"] == [before[0], before[1], before[6]]
    assert m.store.data["reminders"] == m.data["reminders"]
    assert await m.async_clear_reminder_history() == {"deleted": 0}


@pytest.mark.parametrize("bulk", [False, True])
async def test_failed_history_deletion_keeps_memory_and_storage_unchanged(bulk):
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "历史记录", "in_minutes": 60}, "history")
    await m.async_cancel_reminder(r["id"])
    before = deepcopy(m.data)

    async def fail(data):
        raise OSError("synthetic save failure")

    m.store.async_save = fail
    with pytest.raises(OSError):
        if bulk:
            await m.async_clear_reminder_history()
        else:
            await m.async_delete_reminder_history(r["id"])
    assert m.data == before
    assert m.store.data == before


async def test_no_selected_output_does_not_create_unusable_reminder():
    m = manager()
    m.entry.options["outputs"] = []
    await m.async_load()
    with pytest.raises(ValueError, match="播报"):
        await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "x")
    assert m.data["reminders"] == []


async def test_broadcast_runs_in_parallel_and_isolates_failure():
    m = manager()
    await m.async_load()
    m.entry.options["outputs"] = ["text.speaker_play_text", "text.failed_play_text"]
    result = await m.async_broadcast("测试", m.entry.options["outputs"])
    assert result["text.speaker_play_text"] == "delivered"
    assert result["text.failed_play_text"] == "failed"
    assert m.hass.services.peak == 2


async def test_interrupted_delivery_is_not_repeated_on_restart():
    data = models.new_state()
    data["reminders"] = [
        {
            "id": "r",
            "status": "delivering",
            "repeat": "none",
            "at": (models.now() - timedelta(minutes=1)).isoformat(),
        }
    ]
    m = manager(data)
    await m.async_load()
    assert m.data["reminders"][0]["status"] == "failed"


async def test_due_reminder_is_persisted_before_broadcast():
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "喝水", "in_minutes": 1}, "due")
    m.data["reminders"][0]["at"] = (models.now() - timedelta(seconds=1)).isoformat()
    seen = []

    async def broadcast(message, outputs):
        seen.append(m.store.data["reminders"][0]["status"])
        return {outputs[0]: "delivered"}

    m.async_broadcast = broadcast
    await m.async_run_due()
    assert seen == ["delivering"]
    assert m.store.data["reminders"][0]["status"] == "completed"
    assert r["id"] == m.store.data["reminders"][0]["id"]


async def test_weather_uses_configured_location_and_real_response():
    m = manager()
    await m.async_load()
    result = await m.async_weather("家", "daily")
    assert result["location"] == "家"
    assert result["forecast"][0]["temperature"] == 20
    assert result["timezone"] == "Asia/Shanghai"


async def test_unknown_location_does_not_fall_back_to_wrong_place():
    m = manager()
    await m.async_load()
    with pytest.raises(ValueError, match="地点"):
        await m.async_weather("公司", "current")


async def test_each_reminder_can_use_devices_other_than_defaults():
    m = manager()
    await m.async_load()
    first = await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "first")
    second = await m.async_add_reminder(
        {"message": "吃药", "in_minutes": 10, "outputs": ["assist_satellite.voice"]}, "second"
    )
    assert first["outputs"] == ["text.speaker_play_text"]
    assert second["outputs"] == ["assist_satellite.voice"]
    await m.async_settings({"outputs": ["text.failed_play_text"]})
    restored = manager(m.store.data)
    await restored.async_load()
    assert [r["outputs"] for r in restored.data["reminders"]] == [
        ["text.speaker_play_text"], ["assist_satellite.voice"]
    ]


async def test_change_one_reminder_devices_without_changing_other_reminder():
    m = manager()
    await m.async_load()
    first = await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "first")
    second = await m.async_add_reminder({"message": "吃药", "in_minutes": 10}, "second")
    await m.async_update_reminder_outputs(first["id"], ["assist_satellite.voice"])
    assert m.store.data["reminders"][0]["outputs"] == ["assist_satellite.voice"]
    assert m.store.data["reminders"][1]["id"] == second["id"]
    assert m.store.data["reminders"][1]["outputs"] == ["text.speaker_play_text"]
    assert m.settings["outputs"] == ["text.speaker_play_text"]


async def test_due_reminders_send_only_to_their_own_devices():
    m = manager()
    await m.async_load()
    await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "first")
    await m.async_add_reminder(
        {"message": "吃药", "in_minutes": 10, "outputs": ["assist_satellite.voice"]}, "second"
    )
    sent = []

    async def broadcast(message, outputs):
        sent.append((message, list(outputs)))
        return dict.fromkeys(outputs, "delivered")

    m.async_broadcast = broadcast
    for reminder in m.data["reminders"]:
        reminder["at"] = (models.now() - timedelta(seconds=1)).isoformat()
    await m.async_run_due()
    assert sent == [
        ("喝水", ["text.speaker_play_text"]), ("吃药", ["assist_satellite.voice"])
    ]
    assert [r["status"] for r in m.store.data["reminders"]] == ["completed", "completed"]


@pytest.mark.parametrize("outputs", [[], ["weather.home"], ["text.missing_play_text"]])
async def test_invalid_reminder_device_update_keeps_original(outputs):
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "first")
    with pytest.raises(ValueError):
        await m.async_update_reminder_outputs(r["id"], outputs)
    assert m.store.data["reminders"][0]["outputs"] == ["text.speaker_play_text"]


async def test_failed_save_keeps_original_reminder_devices():
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "first")

    async def fail(data):
        raise OSError("synthetic save failure")

    m.store.async_save = fail
    with pytest.raises(OSError):
        await m.async_update_reminder_outputs(r["id"], ["assist_satellite.voice"])
    assert m.data["reminders"][0]["outputs"] == ["text.speaker_play_text"]


@pytest.mark.parametrize("status", ["delivering", "completed", "cancelled", "failed", "missed"])
async def test_only_pending_reminder_can_change_devices(status):
    m = manager()
    await m.async_load()
    r = await m.async_add_reminder({"message": "喝水", "in_minutes": 5}, "first")
    m.data["reminders"][0]["status"] = status
    with pytest.raises(ValueError):
        await m.async_update_reminder_outputs(r["id"], ["assist_satellite.voice"])
