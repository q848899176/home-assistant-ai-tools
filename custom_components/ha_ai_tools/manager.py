"""HA 持久提醒、天气与播报管理。"""

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from . import models

_LOGGER = logging.getLogger(__name__)


class ToolsManager:
    def __init__(self, hass, entry, store=None):
        self.hass, self.entry = hass, entry
        if store is None:
            from homeassistant.helpers.storage import Store

            store = Store(hass, 1, f"ha_ai_tools.{entry.entry_id}")
        self.store = store
        self.data = models.new_state()
        self.lock = asyncio.Lock()
        self.session_locks = {}
        self.function_tasks = set()
        self.active_tasks = set()
        self.profile_lock = asyncio.Lock()
        self._timer = None
        self._started = False
        self._retry_delay = 0

    @property
    def settings(self):
        return {**getattr(self.entry, "data", {}), **self.entry.options, **self.data["settings"]}

    async def async_load(self):
        saved = await self.store.async_load()
        if saved:
            self.data.update(saved)
        if not self.data.get("legacy_imported"):
            legacy = self.hass.states.get("input_text.jarvis_long_memory")
            if (
                legacy
                and legacy.state not in ("", "unknown", "unavailable")
                and models.sanitize(legacy.state) == legacy.state
                and not self.data["memories"]
            ):
                self.data["memories"].append(
                    {
                        "id": uuid4().hex,
                        "key": "原有长期记忆",
                        "text": legacy.state[:1000],
                        "evidence": legacy.state[:1000],
                        "scope": "family",
                        "source": "input_text.jarvis_long_memory",
                        "updated_at": models.now().isoformat(),
                    }
                )
            self.data["legacy_imported"] = True
        for item in self.data["reminders"]:
            at = datetime.fromisoformat(item["at"])
            if item["status"] == "delivering":
                item["status"] = "failed"
                item["error"] = "上次播报被重启中断，请检查结果"
            if (
                item["status"] in ("pending", "failed")
                and item.get("repeat", "none") != "none"
                and at <= models.now()
            ):
                item["at"] = models.next_occurrence(at, item["repeat"]).isoformat()
                item["status"] = "pending"
        await self.async_save()

    async def async_save(self):
        clean = models.sanitize_data(self.data)
        await self.store.async_save(clean)
        self.data = clean

    async def _update(self, change):
        async with self.lock:
            candidate = deepcopy(self.data)
            result = change(candidate)
            candidate = models.sanitize_data(candidate)
            await self.store.async_save(candidate)
            self.data = candidate
            return models.sanitize_data(result)

    async def async_settings(self, settings):
        await self._update(lambda data: data["settings"].update(settings))

    async def async_store_session(self, key, session, generation):
        def change(data):
            if data.get("generation", 0) != generation:
                return False
            data["sessions"][key] = session
            while len(data["sessions"]) > 64:
                oldest = min(data["sessions"], key=lambda k: data["sessions"][k]["updated_at"])
                data["sessions"].pop(oldest)
            return True

        return await self._update(change)

    async def async_prepare_automation(self, fingerprint, request_id):
        def change(data):
            for record in data["requests"].values():
                if record.get("fingerprint") == fingerprint and record.get("status") != "loaded":
                    return record["id"]
            operation_id = "ha_ai_" + uuid5(NAMESPACE_URL, request_id).hex
            data["requests"][operation_id] = {
                "id": operation_id,
                "fingerprint": fingerprint,
                "status": "preparing",
            }
            if len(data["requests"]) > 100:
                data["requests"].pop(next(iter(data["requests"])))
            return operation_id

        return await self._update(change)

    async def async_finish_automation(self, operation_id, loaded):
        def change(data):
            if operation_id in data["requests"]:
                data["requests"][operation_id]["status"] = "loaded" if loaded else "saved"

        await self._update(change)

    def async_start(self):
        self._started = True
        self.async_schedule()

    def async_stop(self):
        self._started = False
        for task in self.function_tasks:
            task.cancel()
        self.function_tasks.clear()
        if self._timer:
            self._timer.cancel()
            self._timer = None

    async def async_shutdown(self):
        tasks = (self.function_tasks | self.active_tasks) - {asyncio.current_task()}
        self.async_stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def async_schedule(self):
        if not self._started:
            return
        if self._timer:
            self._timer.cancel()
        dates = [
            datetime.fromisoformat(r["at"])
            for r in self.data["reminders"]
            if r["status"] == "pending"
        ]
        if not dates:
            self._timer = None
            return
        delay = max(0.1, self._retry_delay, (min(dates) - models.now()).total_seconds())
        self._timer = asyncio.get_running_loop().call_later(
            min(delay, 86400), lambda: self.hass.async_create_task(self.async_run_due())
        )

    def validate_outputs(self, outputs, *, selected_only=True):
        if (
            not isinstance(outputs, list)
            or not outputs
            or len(outputs) > 50
            or any(not isinstance(x, str) for x in outputs)
        ):
            raise ValueError("请先勾选至少一个播报设备")
        allowed = self.settings.get("outputs", [])
        result = []
        for entity_id in dict.fromkeys(outputs):
            state = self.hass.states.get(entity_id)
            valid = state and (
                entity_id.startswith("assist_satellite.")
                and state.attributes.get("supported_features", 0) & 1
                or entity_id.startswith("text.")
                and (
                    entity_id.endswith("_play_text")
                    or "播放文本" in state.attributes.get("friendly_name", "")
                )
            )
            if not valid or selected_only and entity_id not in allowed:
                raise ValueError("播报设备未配置或不支持播报")
            result.append(entity_id)
        return result

    async def async_add_reminder(self, args, request_id, *, test_id=None):
        if test_id is not None:
            runtime = getattr(self, "speech", None)
            if runtime is None or not getattr(runtime, "testing", None):
                raise ValueError("测试身份无法验证")
            run = runtime.store.data["test_runs"].get(test_id)
            defaults = run["targets"] if run else []
            outputs = runtime.testing.validate_targets(test_id, args.get("outputs", defaults))
            args = {**args, "outputs": outputs}
        def change(data):
            for reminder in data["reminders"]:
                if reminder.get("request_id") == request_id:
                    return reminder
            message = models.bounded_text(args.get("message"), "提醒内容", 1000)
            if models.sanitize(message) != message:
                raise ValueError("提醒不能保存密码或密钥")
            outputs = self.validate_outputs(
                args.get("outputs", self.settings.get("outputs", [])), selected_only=False
            )
            repeat = args.get("repeat", "none")
            if repeat not in models.REPEATS:
                raise ValueError("不支持的重复规则")
            at = models.parse_time(args)
            if repeat == "weekdays":
                while at.weekday() >= 5:
                    at += timedelta(days=1)
            item = {
                "id": uuid4().hex,
                "message": message,
                "at": at.isoformat(),
                "repeat": repeat,
                "outputs": outputs,
                "status": "pending",
                "created_at": models.now().isoformat(),
                "request_id": request_id,
            }
            if test_id is not None:
                item["test_id"] = test_id
            data["reminders"].append(item)
            return item

        result = await self._update(change)
        self.async_schedule()
        return result

    async def async_update_reminder_outputs(self, reminder_id, outputs):
        def change(data):
            item = next((r for r in data["reminders"] if r["id"] == reminder_id), None)
            if not item or item["status"] != "pending":
                raise ValueError("只能修改尚未开始播报的提醒")
            if item.get("test_id"):
                self.speech.testing.validate_targets(item["test_id"], outputs)
            item["outputs"] = self.validate_outputs(outputs, selected_only=False)
            return item

        return await self._update(change)

    async def async_cancel_reminder(self, reminder_id):
        def change(data):
            item = next((r for r in data["reminders"] if r["id"] == reminder_id), None)
            if not item or item["status"] != "pending":
                raise ValueError("提醒不存在、已完成或正在播报")
            item["status"] = "cancelled"
            return {"id": reminder_id, "status": "cancelled"}

        result = await self._update(change)
        self.async_schedule()
        return result

    async def async_delete_reminder_history(self, reminder_id):
        if not isinstance(reminder_id, str) or not reminder_id:
            raise ValueError("请选择要删除的历史记录")

        def change(data):
            item = next((r for r in data["reminders"] if r["id"] == reminder_id), None)
            if item is None or item["status"] not in models.REMINDER_HISTORY_STATES:
                raise ValueError("只能删除已结束的提醒记录，待执行和正在播报的提醒会保留")
            data["reminders"].remove(item)
            return {"id": reminder_id, "deleted": 1}

        return await self._update(change)

    async def async_clear_reminder_history(self):
        def change(data):
            before = len(data["reminders"])
            data["reminders"] = [
                r for r in data["reminders"] if r["status"] not in models.REMINDER_HISTORY_STATES
            ]
            return {"deleted": before - len(data["reminders"])}

        return await self._update(change)

    async def async_broadcast(self, message, outputs, *, test_id=None):
        from .speech.broadcast import async_send

        return await async_send(self, message, outputs, test_id=test_id)

    async def async_run_due(self):
        task = asyncio.current_task()
        self.active_tasks.add(task)
        try:
            due = [
                r["id"]
                for r in self.data["reminders"]
                if r["status"] == "pending" and datetime.fromisoformat(r["at"]) <= models.now()
            ]
            for reminder_id in due:

                def claim(data, reminder_id=reminder_id):
                    item = next(r for r in data["reminders"] if r["id"] == reminder_id)
                    if item["status"] != "pending":
                        return None
                    at = datetime.fromisoformat(item["at"])
                    if models.now() - at > timedelta(hours=24):
                        next_at = models.next_occurrence(at, item["repeat"])
                        item["status"] = "pending" if next_at else "missed"
                        if next_at:
                            item["at"] = next_at.isoformat()
                        return None
                    item["status"] = "delivering"
                    return item

                item = await self._update(claim)
                if item is None:
                    continue
                try:
                    if item.get("test_id"):
                        outcomes = await self.async_broadcast(item["message"], item["outputs"], test_id=item["test_id"])
                    else:
                        outcomes = await self.async_broadcast(item["message"], item["outputs"])
                except ValueError:
                    outcomes = dict.fromkeys(item["outputs"], "failed")

                def finish(data, reminder_id=reminder_id, outcomes=outcomes, item=item):
                    current = next(r for r in data["reminders"] if r["id"] == reminder_id)
                    current["delivery"] = outcomes
                    current["last_delivered_at"] = models.now().isoformat()
                    current["status"] = (
                        "completed"
                        if all(x == "delivered" for x in outcomes.values())
                        else "failed"
                    )
                    next_at = models.next_occurrence(
                        datetime.fromisoformat(item["at"]), item["repeat"]
                    )
                    if next_at:
                        current["at"] = next_at.isoformat()
                        current["status"] = "pending"

                await self._update(finish)
            self._retry_delay = 0
        except Exception as err:
            self._retry_delay = 60
            _LOGGER.error("提醒处理未完成，请检查存储与任务状态: %s", type(err).__name__)
        finally:
            self.active_tasks.discard(task)
            self.async_schedule()

    async def async_weather(self, location=None, kind="current"):
        from .weather import weather_sources

        if kind not in ("current", "daily", "hourly"):
            raise ValueError("只支持实况、每日或每小时预报")
        locations = self.settings.get("locations", {})
        location = location or next(iter(locations), None)
        entity_id = locations.get(location)
        if not entity_id:
            raise ValueError("尚未配置此地点的天气，请在 AI 工具页面选择")
        sources = weather_sources(self.hass, entity_id)
        sources.sort(
            key=lambda source: (state := self.hass.states.get(source)) is None
            or state.state in ("unknown", "unavailable")
        )
        errors = []
        try:
            async with asyncio.timeout(20):
                for source in sources:
                    try:
                        result = await self._async_weather_source(location, source, kind)
                    except ValueError as err:
                        errors.append(str(err))
                        continue
                    result["primary_entity_id"] = entity_id
                    result["fallback_used"] = source != entity_id
                    return result
        except TimeoutError:
            raise ValueError(f"“{location}”的天气查询超时，已尝试刷新或查询同地点来源，请稍后重试") from None
        if len(sources) == 1:
            raise ValueError(errors[0])
        raise ValueError(f"“{location}”的同地点天气来源均不可用，已尝试自动切换和刷新，请稍后重试")

    async def _async_weather_source(self, location, entity_id, kind):
        from homeassistant.components.weather.const import WeatherEntityFeature
        from homeassistant.exceptions import HomeAssistantError

        state = self.hass.states.get(entity_id)
        if not state:
            raise ValueError(
                f"“{location}”绑定的天气实体（{entity_id}）已不存在，请在“天气地点”重新选择并保存"
            )
        if state.state in ("unknown", "unavailable"):
            try:
                async with asyncio.timeout(8):
                    await self.hass.services.async_call(
                        "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
                    )
            except (HomeAssistantError, TimeoutError):
                pass
            state = self.hass.states.get(entity_id)
            if not state or state.state in ("unknown", "unavailable"):
                raise ValueError(
                    f"“{location}”的天气服务暂时无法连接（{entity_id}），已尝试刷新。"
                    "请稍后重试，或在“天气地点”选择同一地点的其他可用来源"
                )
        result = {
            "location": location,
            "entity_id": entity_id,
            "timezone": "Asia/Shanghai",
            "updated_at": state.last_updated.astimezone(models.TZ).isoformat(),
            "condition": state.state,
        }
        for key in (
            "temperature",
            "temperature_unit",
            "humidity",
            "wind_speed",
            "wind_speed_unit",
            "pressure",
            "attribution",
        ):
            if key in state.attributes:
                result[key] = state.attributes[key]
        if kind != "current":
            feature = WeatherEntityFeature.FORECAST_DAILY if kind == "daily" else WeatherEntityFeature.FORECAST_HOURLY
            if not state.attributes.get("supported_features", 0) & feature:
                raise ValueError(f"该天气来源不支持所需预报（{entity_id}）")
            try:
                async with asyncio.timeout(10):
                    response = await self.hass.services.async_call(
                        "weather",
                        "get_forecasts",
                        {"entity_id": entity_id, "type": kind},
                        blocking=True,
                        return_response=True,
                    )
            except (HomeAssistantError, TimeoutError):
                raise ValueError(
                    f"“{location}”的天气预报获取失败（{entity_id}），请稍后重试或检查天气集成连接"
                ) from None
            forecasts = (response or {}).get(entity_id, {}).get("forecast")
            if not forecasts:
                raise ValueError("该天气源暂未返回所需预报")
            result["forecast"] = deepcopy(forecasts[: 24 if kind == "hourly" else 7])
            for item in result["forecast"]:
                if item.get("datetime"):
                    item["datetime"] = (
                        datetime.fromisoformat(item["datetime"]).astimezone(models.TZ).isoformat()
                    )
        return models.sanitize_data(result)

    async def async_remember(self, args, user_text, scope, generation=None):
        def change(data):
            if generation is not None and data.get("generation", 0) != generation:
                raise ValueError("记忆已被更新，请重新表达")
            return models.remember(
                data, args.get("key"), args.get("text"), args.get("evidence"), user_text, scope
            )

        return await self._update(change)

    async def async_forget(self, memory_id, scope=None):
        def change(data):
            old = len(data["memories"])
            data["memories"] = [
                m
                for m in data["memories"]
                if not (m["id"] == memory_id and (scope is None or m["scope"] == scope))
            ]
            if old == len(data["memories"]):
                raise ValueError("记忆不存在或不属于当前用户")
            data["sessions"] = {}
            data["generation"] = data.get("generation", 0) + 1
            return {"forgotten": memory_id}

        return await self._update(change)
