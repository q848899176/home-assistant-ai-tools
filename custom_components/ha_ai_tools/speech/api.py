"""语音管理及短期试听音频；所有入口为管理员权限。"""

import asyncio
import io
import wave
from copy import deepcopy
from time import monotonic
from uuid import uuid4

from aiohttp import web
from homeassistant.components.http import KEY_HASS_USER, HomeAssistantView

from ..models import bounded_text
from .config import async_save_speech, list_speech, raw_speech

ACTIONS = {"save_speech", "probe_speech", "list_speech_pipelines", "preview_speech_apply", "apply_speech", "restore_speech",
           "preview_tts", "save_device_voice", "create_speech_test", "finish_speech_test", "add_speech_test_reminder"}


def require_speech_admin(request):
    user = request.get(KEY_HASS_USER)
    if user is None or not user.is_admin:
        raise web.HTTPForbidden(text="仅管理员可以管理和试听语音")
    return user.id


class PreviewCache:
    def __init__(self, ttl=60):
        self.items = {}
        self.handles = {}
        self.ttl = ttl

    def expire(self, identifier):
        self.items.pop(identifier, None)
        handle = self.handles.pop(identifier, None)
        if handle:
            handle.cancel()

    def close(self):
        for identifier in list(self.items):
            self.expire(identifier)

    def prune(self):
        for identifier, item in list(self.items.items()):
            if item["expires"] <= monotonic():
                self.expire(identifier)

    def add(self, owner, data):
        self.prune()
        while self.items and (len(self.items) >= 8 or sum(len(i["data"]) for i in self.items.values()) + len(data) > 32 * 1024 * 1024):
            self.expire(next(iter(self.items)))
        if len(data) > 32 * 1024 * 1024 or not owner:
            raise ValueError("试听音频过大或身份无效")
        identifier = uuid4().hex
        self.items[identifier] = {"owner": owner, "data": data, "expires": monotonic() + self.ttl}
        try:
            self.handles[identifier] = asyncio.get_running_loop().call_later(self.ttl, self.expire, identifier)
        except RuntimeError:
            pass
        return identifier

    def get(self, identifier, owner):
        self.prune()
        item = self.items.get(identifier)
        if item is None:
            raise web.HTTPNotFound(text="试听已过期，请重新生成")
        if item["owner"] != owner:
            raise web.HTTPForbidden(text="试听音频仅对创建者开放")
        return item["data"]


def _runtime(manager):
    runtime = getattr(manager, "speech", None)
    if runtime is None or getattr(runtime, "error", ""):
        raise ValueError("语音管理尚未就绪，请检查本地服务和恢复记录")
    return runtime


def _tts_entity(runtime, identifier):
    entity = runtime.entities.get((identifier, "tts"))
    if entity is None:
        raise ValueError("合成实体尚未加载，请保存配置并刷新")
    return entity


def _pipeline_list(manager):
    from homeassistant.components.assist_pipeline import async_get_pipelines

    return [p.to_json() for p in async_get_pipelines(manager.hass)]


def _validate_changes(manager, changes):
    if not getattr(manager, "hass", None):
        return
    from homeassistant.components import stt
    from homeassistant.components.tts.helper import get_engine_instance

    for item in changes.get("pipelines", []):
        current = manager.speech.pipelines._read(item["id"])
        after = {**current, **item["fields"]}
        for cap in ("stt", "tts"):
            identifier = after.get(cap + "_engine")
            if not identifier:
                continue
            engine = stt.async_get_speech_to_text_engine(manager.hass, identifier) if cap == "stt" else get_engine_instance(manager.hass, identifier)
            if engine is None or after.get(cap + "_language") not in engine.supported_languages:
                raise ValueError("语音引擎或语言不存在，请刷新可用配置")
            if cap == "tts" and after.get("tts_voice") and hasattr(engine, "async_get_supported_voices"):
                voices = engine.async_get_supported_voices(after["tts_language"])
                if voices is not None and after["tts_voice"] not in {v.voice_id for v in voices}:
                    raise ValueError("此合成引擎不支持所选音色")
    for entity, options in changes.get("device_voices", {}).items():
        if options is None:
            continue
        manager.validate_outputs([entity], selected_only=False)
        if not isinstance(options, dict) or set(options) - {"tts_entity_id", "language", "voice", "speed"}:
            raise ValueError("设备音色参数无效")
        engine = get_engine_instance(manager.hass, options.get("tts_entity_id", ""))
        if engine is None or not options.get("voice") or options.get("language", "zh") not in engine.supported_languages:
            raise ValueError("设备合成引擎、语言或音色无效")
        voices = engine.async_get_supported_voices(options.get("language", "zh")) if hasattr(engine, "async_get_supported_voices") else None
        if voices is not None and options["voice"] not in {v.voice_id for v in voices}:
            raise ValueError("此设备所选引擎不支持该音色")
        speed = options.get("speed", 1.0)
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not .5 <= speed <= 2:
            raise ValueError("语速无效")


async def _delete_speech(manager, identifier):
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(manager.hass)
    value = next((s for s in raw_speech(manager.entry) if s["id"] == identifier), None)
    if value is None:
        raise ValueError("语音配置不存在")
    entities = {registry.async_get_entity_id(cap, "ha_ai_tools", f"{manager.entry.entry_id}:speech:{identifier}:{cap}") for cap in ("stt", "tts")}
    entities.discard(None)
    if any(p["stt_engine"] in entities or p["tts_engine"] in entities for p in _pipeline_list(manager)):
        raise ValueError("语音引擎仍被流水线引用，请先恢复或更换引擎")
    if any(v["tts_entity_id"] in entities for v in manager.speech.store.data["device_voices"].values()):
        raise ValueError("语音引擎仍被设备音色引用，请先恢复设备配置")
    manager.hass.config_entries.async_remove_subentry(manager.entry, value["subentry_id"])
    return {"deleted": True, "id": identifier}


async def async_action(manager, action, payload, *, actor=None):
    runtime = _runtime(manager)
    if action == "save_speech":
        if payload.get("delete"):
            return await _delete_speech(manager, payload.get("id"))
        return await async_save_speech(manager, payload)
    if action == "probe_speech":
        return await runtime.get_client(payload.get("id")).async_probe()
    if action == "list_speech_pipelines":
        return {"pipelines": _pipeline_list(manager)}
    if action == "preview_speech_apply":
        _validate_changes(manager, payload)
        return await runtime.pipelines.async_preview(payload)
    if action == "apply_speech":
        point = runtime.pipelines._point(payload.get("preview_id"))
        _validate_changes(manager, point["changes"])
        return await runtime.pipelines.async_apply(payload.get("preview_id"), payload.get("fingerprint"))
    if action == "restore_speech":
        return await runtime.pipelines.async_restore(payload.get("restore_id"), payload.get("resolutions"))
    if action == "save_device_voice":
        changes = {"device_voices": {payload.get("entity_id"): payload.get("options")}}
        _validate_changes(manager, changes)
        return await runtime.pipelines.async_preview(changes)
    if action == "preview_tts":
        if not actor:
            raise ValueError("试听需要有效的管理员身份")
        entity = _tts_entity(runtime, payload.get("id"))
        text = bounded_text(payload.get("text", "现在是语音试听，请选择您喜欢的声音。"), "试听内容", 1000)
        started = monotonic()
        extension, audio = await entity.async_get_tts_audio(text, "zh", {"voice": payload.get("voice", entity.config["voice"]), "speed": payload.get("speed", 1.0)})
        if extension != "wav":
            raise ValueError("试听音频格式不支持")
        if not hasattr(runtime, "previews"):
            runtime.previews = PreviewCache()
        identifier = runtime.previews.add(actor, audio)
        with wave.open(io.BytesIO(audio), "rb") as f:
            seconds = f.getnframes() / f.getframerate()
        return {"audio_id": identifier, "complete_ms": round((monotonic() - started) * 1000),
                "audio_seconds": round(seconds, 3), "delivery": "完整WAV，非实际设备首声"}
    if action == "create_speech_test":
        run = await runtime.testing.async_create_run(payload.get("entity_id"))
        if payload.get("source_pipeline_id"):
            if run.get("pipeline_id"):
                return run
            return {**run, **await runtime.pipelines.async_prepare_test(run["id"], payload["source_pipeline_id"], run["targets"][0])}
        return run
    if action == "add_speech_test_reminder":
        run = runtime.store.data["test_runs"].get(payload.get("test_id"))
        if not run:
            raise ValueError("测试会话不存在")
        args = {k: payload[k] for k in ("message", "at", "in_minutes", "repeat", "outputs") if k in payload}
        return await manager.async_add_reminder(args, uuid4().hex, test_id=run["id"])
    if action == "finish_speech_test":
        identifier = payload.get("test_id")
        run = runtime.store.data["test_runs"].get(identifier)
        if not run:
            raise ValueError("测试会话不存在")
        if run.get("restore_id"):
            restored = await runtime.pipelines.async_restore(run["restore_id"])
            if restored["status"] != "restored":
                return restored
        for item in list(manager.data["reminders"]):
            if item.get("test_id") == identifier and item["status"] == "pending":
                await manager.async_cancel_reminder(item["id"])
        if run.get("pipeline_id"):
            from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE

            collection = manager.hass.data[KEY_ASSIST_PIPELINE].pipeline_store
            candidate = collection.data.get(run["pipeline_id"])
            if candidate and candidate.name == "AI Tools Nabu 验证 " + identifier[:8]:
                if not any(s.state == candidate.name for s in manager.hass.states.async_all("select")):
                    await collection.async_delete_item(candidate.id)
        await runtime.testing.async_finish_run(identifier)
        def clean(data):
            data["reminders"] = [r for r in data["reminders"] if r.get("test_id") != identifier or r["status"] == "delivering"]
            data["sessions"] = {k: v for k, v in data["sessions"].items() if v.get("test_id") != identifier}
        await manager._update(clean)
        return {"test_id": identifier, "status": "closed", "restored": True}
    raise ValueError("未知语音管理操作")


def snapshot(manager):
    runtime = getattr(manager, "speech", None)
    if runtime is None:
        return {"configs": [], "pipelines": [], "nabu_targets": []}
    store = getattr(runtime, "store", None)
    targets = []
    if getattr(runtime, "testing", None):
        for state in manager.hass.states.async_all("assist_satellite"):
            try:
                runtime.testing._device(state.entity_id)
                targets.append({"entity_id": state.entity_id, "name": state.name, "available": state.state not in ("unknown", "unavailable")})
            except ValueError:
                pass
    try:
        pipelines = _pipeline_list(manager)
    except (KeyError, ValueError):
        pipelines = []
    configs = list_speech(manager.entry)
    for config in configs:
        for cap in ("stt", "tts"):
            entity = getattr(runtime, "entities", {}).get((config["id"], cap))
            config[cap + "_entity_id"] = entity.entity_id if entity else None
    return {"configs": configs, "pipelines": pipelines, "nabu_targets": targets,
            "device_voices": deepcopy(store.data["device_voices"]) if store else {}, "error": getattr(runtime, "error", ""),
            "test_runs": [deepcopy(r) for r in store.data["test_runs"].values() if r["status"] == "active"] if store else [],
            "restore_points": [{"id": p["id"], "at": p["at"], "status": store.data["applications"].get(p["id"], {}).get("status", "preview")}
                               for p in store.data["restore_points"].values()][-50:][::-1] if store else [],
            "cache_notice": "AI Tools音色试听及使用自定义音色的提醒不写磁盘缓存；原播报方式和Assist对话遵循HA现有缓存策略。"}


async def read_upload(request):
    form = await request.multipart()
    data, speech_id, content_type = bytearray(), "", "audio/wav"
    async for part in form:
        if part.name == "speech_id" and not part.filename:
            value = await part.read_chunk(1024)
            if len(value) > 256:
                raise ValueError("语音配置标识过长")
            speech_id = value.decode()
        elif part.name == "file" and part.filename:
            content_type = part.headers.get("Content-Type", "audio/wav")
            while chunk := await part.read_chunk():
                data.extend(chunk)
                if len(data) > 20 * 1024 * 1024:
                    raise ValueError("音频超过二十MiB限制")
        else:
            raise ValueError("上传字段不支持")
    if not speech_id or not data:
        raise ValueError("请选择语音配置并上传音频")
    return speech_id, bytes(data), content_type


def async_register_views(hass):
    class SpeechAudioView(HomeAssistantView):
        url = "/api/ha_ai_tools/speech/audio/{audio_id}"
        name = "api:ha_ai_tools:speech:audio"
        requires_auth = True

        async def get(self, request, audio_id):
            owner = require_speech_admin(request)
            manager = hass.data.get("ha_ai_tools")
            cache = getattr(getattr(manager, "speech", None), "previews", None)
            if cache is None:
                raise web.HTTPNotFound()
            return web.Response(body=cache.get(audio_id, owner), content_type="audio/wav", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    class SpeechSTTView(HomeAssistantView):
        url = "/api/ha_ai_tools/speech/stt_test"
        name = "api:ha_ai_tools:speech:stt_test"
        requires_auth = True

        async def post(self, request):
            require_speech_admin(request)
            from .audio import async_prepare_upload

            started = monotonic()
            try:
                identifier, data, content_type = await read_upload(request)
                manager = hass.data.get("ha_ai_tools")
                runtime = _runtime(manager)
                audio = await async_prepare_upload(hass, data, content_type)
                text = await runtime.get_client(identifier).async_transcribe(audio, language="zh")
                return web.json_response({"text": text, "complete_ms": round((monotonic() - started) * 1000), "measurement": "含上传、转换及识别，不代表VAD等待"}, headers={"Cache-Control": "no-store"})
            except (ValueError, TimeoutError):
                return web.json_response({"error": "识别测试失败，请检查音频格式、时长和本地服务"}, status=400)

    hass.http.register_view(SpeechAudioView())
    hass.http.register_view(SpeechSTTView())
