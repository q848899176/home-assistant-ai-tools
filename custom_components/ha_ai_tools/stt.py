"""AI Tools 本地语音识别实体。"""

from homeassistant.components import stt

from .speech.audio import async_collect_pcm, pcm_to_wav


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = entry.runtime_data.speech
    runtime.adders["stt"] = async_add_entities
    for identifier, config in runtime.configs.items():
        if "stt" in config["capabilities"]:
            async_add_entities([AIToolsSTTEntity(identifier, runtime)], config_subentry_id=config["subentry_id"])


class AIToolsSTTEntity(stt.SpeechToTextEntity):
    def __init__(self, speech_id, runtime):
        self.speech_id, self.runtime = speech_id, runtime
        self._attr_unique_id = f"{runtime.entry.entry_id}:speech:{speech_id}:stt"
        self._attr_name = runtime.configs[speech_id]["name"] + " 语音识别"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.runtime.entities[(self.speech_id, "stt")] = self

    async def async_will_remove_from_hass(self):
        self.runtime.entities.pop((self.speech_id, "stt"), None)
        await super().async_will_remove_from_hass()

    @property
    def supported_languages(self):
        return ["zh", "zh-CN", "zh_CN"]

    @property
    def supported_formats(self):
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self):
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self):
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self):
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self):
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(self, metadata, stream):
        try:
            pcm = await async_collect_pcm(stream, metadata)
            if not any(pcm):
                return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
            audio = pcm_to_wav(pcm, sample_rate=16000, sample_width=2, channels=1)
            text = await self.runtime.get_client(self.speech_id).async_transcribe(audio, language=metadata.language)
            if text:
                return stt.SpeechResult(text, stt.SpeechResultState.SUCCESS)
        except (ValueError, TimeoutError):
            pass
        return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
