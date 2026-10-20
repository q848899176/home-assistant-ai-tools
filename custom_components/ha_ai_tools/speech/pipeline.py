"""私有恢复记录与字段级流水线更新；不编辑原始.storage。"""

import asyncio
import hashlib
import json
from copy import deepcopy
from uuid import uuid4

from ..models import now

FIELDS = {"stt_engine", "stt_language", "tts_engine", "tts_language", "tts_voice"}
VOICE_FIELDS = {"tts_entity_id", "language", "voice", "speed"}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class SpeechStore:
    def __init__(self, hass, root_entry_id, store=None):
        if store is None:
            from homeassistant.helpers.storage import Store

            store = Store(hass, 1, f"ha_ai_tools.speech.{root_entry_id}")
        self.store, self.lock = store, asyncio.Lock()
        self.data = {"device_voices": {}, "test_policy": {}, "test_runs": {}, "restore_points": {}, "applications": {}}

    async def async_load(self):
        saved = await self.store.async_load()
        if saved:
            self.data.update(saved)
        return self.data

    async def async_update(self, change):
        async with self.lock:
            candidate = deepcopy(self.data)
            result = change(candidate)
            await self.store.async_save(candidate)
            self.data = candidate
            return deepcopy(result)


class PipelineManager:
    def __init__(self, hass, store):
        self.hass, self.store, self.lock = hass, store, asyncio.Lock()

    def _read(self, identifier):
        from homeassistant.components.assist_pipeline import async_get_pipeline

        return async_get_pipeline(self.hass, identifier).to_json()

    async def _write(self, identifier, fields):
        from homeassistant.components.assist_pipeline import (
            async_get_pipeline,
            async_update_pipeline,
        )

        await async_update_pipeline(self.hass, async_get_pipeline(self.hass, identifier), **fields)

    async def async_preview(self, changes):
        if not isinstance(changes, dict) or set(changes) - {"pipelines", "device_voices"}:
            raise ValueError("语音变更格式无效")
        pipelines, devices = changes.get("pipelines", []), changes.get("device_voices", {})
        if not isinstance(pipelines, list) or not isinstance(devices, dict) or len(pipelines) > 20 or len(devices) > 50:
            raise ValueError("语音变更对象过多或格式无效")
        before, seen = {}, set()
        for item in pipelines:
            identifier, fields = item.get("id"), item.get("fields")
            if not isinstance(identifier, str) or identifier in seen or not isinstance(fields, dict) or not fields or set(fields) - FIELDS:
                raise ValueError("只能修改所选流水线的语音字段")
            if any(v is not None and (not isinstance(v, str) or len(v) > 256) for v in fields.values()):
                raise ValueError("语音字段格式无效")
            seen.add(identifier)
            before[identifier] = self._read(identifier)
        for entity, options in devices.items():
            if not isinstance(entity, str) or not entity.startswith("assist_satellite."):
                raise ValueError("设备音色仅支持语音卫星")
            if options is not None and (not isinstance(options, dict) or set(options) - VOICE_FIELDS):
                raise ValueError("设备音色参数无效")
        baseline = {"pipelines": before, "device_voices": {e: deepcopy(self.store.data["device_voices"].get(e)) for e in devices}}
        identifier = uuid4().hex
        point = {"id": identifier, "at": now().isoformat(), "before": baseline, "changes": deepcopy(changes)}
        point["fingerprint"] = fingerprint({"before": baseline, "changes": changes})
        await self.store.async_update(lambda d: d["restore_points"].__setitem__(identifier, point))
        return deepcopy(point)

    def _point(self, identifier):
        point = self.store.data["restore_points"].get(identifier)
        if point is None:
            raise ValueError("恢复点不存在")
        return deepcopy(point)

    async def _status(self, identifier, status, **extras):
        def change(data):
            data["applications"][identifier] = {**data["applications"].get(identifier, {}), "status": status,
                                                "updated_at": now().isoformat(), **extras}
        await self.store.async_update(change)

    async def async_apply(self, preview_id, fingerprint):
        async with self.lock:
            point = self._point(preview_id)
            if point["fingerprint"] != fingerprint:
                raise ValueError("预览标识已改变，请重新预览")
            status = self.store.data["applications"].get(preview_id, {}).get("status")
            if status == "restored":
                raise ValueError("此恢复点已结束，请重新预览")
            if status == "applied":
                for item in point["changes"].get("pipelines", []):
                    if any(self._read(item["id"]).get(k) != v for k, v in item["fields"].items()):
                        raise ValueError("已应用配置后来发生变化，请重新预览")
                if any(self.store.data["device_voices"].get(e) != value for e, value in point["changes"].get("device_voices", {}).items()):
                    raise ValueError("设备音色后来发生变化，请重新预览")
                return {"applied": True, "restore_id": preview_id, "status": "applied"}
            for identifier, original in point["before"]["pipelines"].items():
                if self._read(identifier) != original:
                    raise ValueError("流水线已由其他操作修改，请重新预览")
            for entity, original in point["before"]["device_voices"].items():
                if self.store.data["device_voices"].get(entity) != original:
                    raise ValueError("设备音色已改变，请重新预览")
            await self._status(preview_id, "prepared")
            try:
                for item in point["changes"].get("pipelines", []):
                    await self._status(preview_id, "writing", current_pipeline=item["id"])
                    if self._read(item["id"]) != point["before"]["pipelines"][item["id"]]:
                        raise ValueError("流水线在应用期间发生变化")
                    await self._write(item["id"], item["fields"])
                    after = self._read(item["id"])
                    if any(after[k] != v for k, v in item["fields"].items()):
                        raise ValueError("配置回读不一致")
                def apply_devices(data):
                    for entity, options in point["changes"].get("device_voices", {}).items():
                        if options is None:
                            data["device_voices"].pop(entity, None)
                        else:
                            data["device_voices"][entity] = options
                await self.store.async_update(apply_devices)
                await self._status(preview_id, "applied")
                return {"applied": True, "restore_id": preview_id, "status": "applied"}
            except (Exception, asyncio.CancelledError) as error:
                await self._status(preview_id, "recovery_required")
                restored = await asyncio.shield(self._restore_locked(preview_id))
                if isinstance(error, asyncio.CancelledError):
                    raise
                return {"applied": False, "restore_id": preview_id, "status": restored["status"], "error": "配置未全部应用，请检查恢复结果"}

    async def async_restore(self, restore_id, resolutions=None):
        async with self.lock:
            return await self._restore_locked(restore_id, resolutions or {})

    async def _restore_locked(self, restore_id, resolutions=None):
        point, conflicts = self._point(restore_id), []
        status = self.store.data["applications"].get(restore_id, {}).get("status")
        if status is None:
            raise ValueError("此预览尚未应用，无需恢复")
        if status == "restored":
            return {"restore_id": restore_id, "status": "restored", "already_restored": True, "conflicts": []}
        resolutions = resolutions or {}
        if any(v not in ("keep_current", "restore_before") for v in resolutions.values()):
            raise ValueError("恢复冲突处理选项无效")
        for item in point["changes"].get("pipelines", []):
            identifier, fields = item["id"], item["fields"]
            try:
                current = self._read(identifier)
                updates = {}
                for key, expected in fields.items():
                    previous = point["before"]["pipelines"][identifier].get(key)
                    choice = resolutions.get(f"{identifier}.{key}")
                    if current.get(key) == previous or choice == "keep_current":
                        continue
                    if current.get(key) == expected or choice == "restore_before":
                        updates[key] = previous
                    else:
                        conflicts.append({"target": identifier, "field": key, "before": previous, "current": current.get(key), "applied": expected})
                if updates:
                    await self._write(identifier, updates)
                    reread = self._read(identifier)
                    if any(reread.get(k) != v for k, v in updates.items()):
                        conflicts.append({"target": identifier, "error": "restore_readback_failed"})
            except Exception:
                conflicts.append({"target": identifier, "error": "restore_target_unavailable"})
        def restore_devices(data):
            for entity, expected in point["changes"].get("device_voices", {}).items():
                current, previous = data["device_voices"].get(entity), point["before"]["device_voices"].get(entity)
                choice = resolutions.get(f"device.{entity}")
                if current == previous or choice == "keep_current":
                    continue
                if current == expected or choice == "restore_before":
                    if previous is None:
                        data["device_voices"].pop(entity, None)
                    else:
                        data["device_voices"][entity] = previous
                else:
                    conflicts.append({"target": entity, "field": "device_voice", "before": previous, "current": current, "applied": expected})
        await self.store.async_update(restore_devices)
        binding = point.get("binding")
        if binding:
            try:
                state = self.hass.states.get(binding["entity_id"])
                if state and state.state == binding["after"]:
                    await self.hass.services.async_call("select", "select_option", {"entity_id": binding["entity_id"], "option": binding["before"]}, blocking=True)
                elif not state or state.state != binding["before"]:
                    conflicts.append({"target": binding["entity_id"], "field": "pipeline_binding"})
            except Exception:
                conflicts.append({"target": binding["entity_id"], "error": "binding_restore_failed"})
        status = "conflict" if conflicts else "restored"
        await self._status(restore_id, status, conflicts=conflicts)
        return {"restore_id": restore_id, "status": status, "conflicts": conflicts}

    async def async_recover_pending(self):
        pending = [identifier for identifier, value in self.store.data["applications"].items() if value["status"] in ("prepared", "writing", "recovery_required")]
        return [await self.async_restore(identifier) for identifier in pending]

    async def async_prepare_test(self, test_id, source_pipeline_id, nabu_entity_id):
        async with self.lock:
            return await self._prepare_test_locked(test_id, source_pipeline_id, nabu_entity_id)

    async def _prepare_test_locked(self, test_id, source_pipeline_id, nabu_entity_id):
        from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        satellite = registry.async_get(nabu_entity_id)
        if satellite is None or satellite.device_id is None:
            raise ValueError("Nabu设备注册信息不存在")
        pipeline_data = self.hass.data[KEY_ASSIST_PIPELINE]
        info = pipeline_data.pipeline_devices.get(satellite.device_id)
        if info is None:
            raise ValueError("Nabu未注册流水线选择器")
        selector = registry.async_get_entity_id("select", info.domain, f"{info.unique_id_prefix}-pipeline")
        original = self.hass.states.get(selector) if selector else None
        if original is None or original.state in ("unknown", "unavailable"):
            raise ValueError("Nabu流水线选择器不可用")
        fields = self._read(source_pipeline_id)
        fields.pop("id")
        fields["name"] = "AI Tools Nabu 验证 " + test_id[:8]
        record = {"id": test_id, "at": now().isoformat(), "before": {"pipelines": {}, "device_voices": {}}, "changes": {},
                  "binding": {"entity_id": selector, "before": original.state, "after": fields["name"]}}
        await self.store.async_update(lambda d: d["restore_points"].__setitem__(test_id, record))
        await self._status(test_id, "prepared")
        try:
            created = await pipeline_data.pipeline_store.async_create_item(fields)
            await self.store.async_update(lambda d: d["test_runs"][test_id].update(pipeline_id=created.id, restore_id=test_id))
            await self._status(test_id, "writing")
            for _ in range(60):
                current = self.hass.states.get(selector)
                if current and fields["name"] in current.attributes.get("options", []):
                    break
                await asyncio.sleep(.05)
            current = self.hass.states.get(selector)
            if current is None or current.state != record["binding"]["before"]:
                raise ValueError("Nabu绑定已由其他操作修改，请重新检查")
            await self.hass.services.async_call("select", "select_option", {"entity_id": selector, "option": fields["name"]}, blocking=True)
            if self.hass.states.get(selector).state != fields["name"]:
                raise ValueError("Nabu测试绑定回读失败")
            await self._status(test_id, "applied")
            return {"test_id": test_id, "pipeline_id": created.id, "restore_id": test_id}
        except (Exception, asyncio.CancelledError) as error:
            await asyncio.shield(self._restore_locked(test_id))
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ValueError("测试流水线准备失败，已尝试恢复原绑定") from None
