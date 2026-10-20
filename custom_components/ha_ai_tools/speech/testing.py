"""后端持久测试身份；未识别或已结束的测试绝不降级为生产播报。"""

from datetime import datetime, timedelta
from uuid import uuid4

from ..models import now


class TestPolicy:
    __test__ = False

    def __init__(self, hass, store):
        self.hass, self.store = hass, store

    def _device(self, entity_id):
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        if not isinstance(entity_id, str) or not entity_id.startswith("assist_satellite."):
            raise ValueError("测试仅允许Nabu语音卫星")
        entity = er.async_get(self.hass).async_get(entity_id)
        device = dr.async_get(self.hass).async_get(entity.device_id) if entity and entity.device_id else None
        manufacturer = (getattr(device, "manufacturer", "") or "").lower()
        model = (getattr(device, "model", "") or "").lower()
        if not device or manufacturer not in ("nabu casa", "home assistant") or "voice" not in model:
            raise ValueError("设备硬件信息不属于Nabu，禁止测试")
        return entity.device_id

    async def async_create_run(self, nabu_entity_id):
        device_id = self._device(nabu_entity_id)
        for run in self.store.data["test_runs"].values():
            if run["device_id"] == device_id and run["status"] == "active":
                if datetime.fromisoformat(run["expires_at"]) <= now():
                    raise ValueError("旧测试已过期，请先恢复并结束旧测试")
                return run.copy()
        identifier = uuid4().hex
        run = {"id": identifier, "device_id": device_id, "targets": [nabu_entity_id], "status": "active",
               "created_at": now().isoformat(), "expires_at": (now() + timedelta(hours=4)).isoformat()}
        def change(data):
            data["test_runs"][identifier] = run
            data["test_policy"]["allowed_targets"] = [nabu_entity_id]
        await self.store.async_update(change)
        return run.copy()

    def validate_targets(self, test_id, outputs):
        run = self.store.data["test_runs"].get(test_id)
        if not run or run["status"] != "active" or datetime.fromisoformat(run["expires_at"]) <= now():
            raise ValueError("测试会话无效或已结束，未进行播报")
        if not isinstance(outputs, list) or not outputs or any(e not in run["targets"] for e in outputs):
            raise ValueError("测试目标必须全部为本次选定的Nabu，未进行播报")
        if any(self._device(e) != run["device_id"] for e in outputs):
            raise ValueError("Nabu设备身份已变化，未进行播报")
        return list(dict.fromkeys(outputs))

    def test_id_for_device(self, device_id):
        for run in self.store.data["test_runs"].values():
            if run["device_id"] == device_id and run["status"] == "active":
                self.validate_targets(run["id"], run["targets"])
                return run["id"]
        return None

    async def async_finish_run(self, test_id):
        if test_id not in self.store.data["test_runs"]:
            raise ValueError("测试会话不存在")
        await self.store.async_update(lambda d: d["test_runs"][test_id].update(status="closed", closed_at=now().isoformat()))
