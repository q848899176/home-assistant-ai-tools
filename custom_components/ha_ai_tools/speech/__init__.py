"""独立语音客户端的生命周期，不重建业务调度器。"""

import asyncio
import hashlib
import json

from .client import SpeechClient
from .config import raw_speech


def model_fingerprint(entry):
    data = {"data": getattr(entry, "data", {}), "options": getattr(entry, "options", {}),
            "subentries": {s.subentry_id: dict(s.data) for s in getattr(entry, "subentries", {}).values() if s.subentry_type != "speech"}}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


class SpeechRuntime:
    def __init__(self, hass, entry, tools_manager):
        self.hass, self.entry, self.manager = hass, entry, tools_manager
        self.clients = {}
        self.lock = asyncio.Lock()
        self.store = None
        self.entities = {}
        self.adders = {}
        self.model_fingerprint = model_fingerprint(entry)
        self.error = ""

    async def async_load(self):
        self.configs = {s["id"]: s for s in raw_speech(self.entry)}
        if self.store is None and self.hass is not None and hasattr(self.hass, "config"):
            from .pipeline import PipelineManager, SpeechStore

            self.store = SpeechStore(self.hass, self.entry.entry_id)
            await self.store.async_load()
            self.pipelines = PipelineManager(self.hass, self.store)
            from .testing import TestPolicy

            self.testing = TestPolicy(self.hass, self.store)
            await self.pipelines.async_recover_pending()

    def get_client(self, speech_id):
        if speech_id not in self.configs:
            raise ValueError("语音配置不存在")
        if speech_id not in self.clients:
            self.clients[speech_id] = SpeechClient(self.configs[speech_id])
        return self.clients[speech_id]

    async def async_close(self):
        if getattr(self, "previews", None):
            self.previews.close()
        clients, self.clients = self.clients, {}
        await asyncio.gather(*(c.async_close() for c in clients.values()))


async def async_refresh(manager, changed_ids):
    runtime = manager.speech
    async with runtime.lock:
        for identifier in changed_ids:
            client = runtime.clients.pop(identifier, None)
            if client:
                await client.async_close()
            for key, entity in list(runtime.entities.items()):
                if key[0] == identifier:
                    await entity.async_remove()
                    runtime.entities.pop(key, None)
        await runtime.async_load()
        for identifier in changed_ids:
            config = runtime.configs.get(identifier)
            if not config:
                continue
            for cap in config["capabilities"]:
                if cap not in runtime.adders:
                    continue
                if cap == "stt":
                    from ..stt import AIToolsSTTEntity as Entity
                else:
                    from ..tts import AIToolsTTSEntity as Entity
                runtime.adders[cap]([Entity(identifier, runtime)], config_subentry_id=config["subentry_id"])
