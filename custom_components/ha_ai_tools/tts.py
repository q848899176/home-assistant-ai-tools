"""AI Tools 多音色语音合成。"""

from homeassistant.components.tts import TextToSpeechEntity, Voice
from homeassistant.components.tts.entity import TTSAudioResponse
from homeassistant.exceptions import HomeAssistantError

from .speech.audio import merge_wavs, split_text

VOICES = {"Vivian": "Vivian · 清亮女声", "Serena": "Serena · 温柔女声", "Uncle_Fu": "Uncle Fu · 成熟男声",
          "Dylan": "Dylan · 北京男声", "Eric": "Eric · 四川男声"}


def media_options():
    """AI Tools 自己创建的媒体明确禁止持久缓存。"""
    return {"cache": False}


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = entry.runtime_data.speech
    runtime.adders["tts"] = async_add_entities
    for identifier, config in runtime.configs.items():
        if "tts" in config["capabilities"]:
            async_add_entities([AIToolsTTSEntity(identifier, runtime)], config_subentry_id=config["subentry_id"])


class AIToolsTTSEntity(TextToSpeechEntity):
    _attr_default_language = "zh"
    _attr_supported_languages = ["zh", "zh-CN", "zh_CN"]
    _attr_supported_options = ["voice", "speed", "config_revision", "preferred_format"]

    def __init__(self, speech_id, runtime):
        self.speech_id, self.runtime = speech_id, runtime
        self.config = runtime.configs[speech_id]
        self._attr_unique_id = f"{runtime.entry.entry_id}:speech:{speech_id}:tts"
        self._attr_name = self.config["name"] + " 语音合成"
        self._attr_default_options = {"voice": self.config["voice"], "speed": self.config.get("speed", 1.0),
                                      "config_revision": self.config.get("config_revision", ""), "preferred_format": "wav"}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.runtime.entities[(self.speech_id, "tts")] = self

    async def async_will_remove_from_hass(self):
        self.runtime.entities.pop((self.speech_id, "tts"), None)
        await super().async_will_remove_from_hass()

    def async_get_supported_voices(self, language):
        return [Voice(identifier, name) for identifier, name in VOICES.items()] if language in self.supported_languages else []

    def async_supports_streaming_input(self):
        return self.config.get("verified", {}).get("streaming") is True

    def _options(self, language, options):
        options = {**self.default_options, **(options or {})}
        if language not in self.supported_languages or options["voice"] not in VOICES:
            raise HomeAssistantError("语音语言或音色不支持")
        speed = options.get("speed", 1.0)
        if isinstance(speed, bool) or not isinstance(speed, (float, int)) or not 0.5 <= speed <= 2:
            raise HomeAssistantError("语速无效")
        if speed != 1.0 and not self.config.get("verified", {}).get("speed_control"):
            raise HomeAssistantError("当前模型未验证变速能力")
        if options.get("config_revision") != self.config.get("config_revision", ""):
            raise HomeAssistantError("语音配置已更新，请刷新后重试")
        return options

    async def async_get_tts_audio(self, message, language, options=None):
        options = self._options(language, options)
        if not isinstance(message, str) or not message.strip() or len(message) > 10000:
            raise HomeAssistantError("播报内容为空或超过一万字")
        values = []
        try:
            for text in split_text(message):
                extension, data = await self.runtime.get_client(self.speech_id).async_synthesize(text, language=language, options=options)
                if extension != "wav":
                    raise ValueError
                values.append(data)
            return "wav", merge_wavs(values)
        except (ValueError, TimeoutError):
            raise HomeAssistantError("语音合成失败，请检查本地服务或恢复原配置") from None

    async def async_stream_tts_audio(self, request):
        if not self.async_supports_streaming_input():
            return await super().async_stream_tts_audio(request)
        text = "".join([chunk async for chunk in request.message_gen])
        options = self._options(request.language, request.options)
        if len(text) > 1000:
            extension, data = await self.async_get_tts_audio(text, request.language, options)

            async def complete():
                yield data

            return TTSAudioResponse(extension, complete())
        extension, upstream = await self.runtime.get_client(self.speech_id).async_stream_synthesize(text, language=request.language, options=options)

        async def chunks():
            try:
                async for chunk in upstream:
                    yield chunk
            finally:
                await upstream.aclose()

        return TTSAudioResponse(extension, chunks())
