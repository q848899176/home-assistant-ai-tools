"""有界的本地音频HTTP协议，不重定向凭据、不自动换服务。"""

import io
import wave

import httpx

MAX_TTS_BYTES = 32 * 1024 * 1024


def check_wav(data):
    try:
        with wave.open(io.BytesIO(data), "rb") as f:
            frames = f.getnframes()
            width = f.getsampwidth() * f.getnchannels()
            if not frames or frames * width > MAX_TTS_BYTES or len(f.readframes(frames)) != frames * width:
                raise ValueError
    except (ValueError, EOFError, wave.Error):
        raise ValueError("语音服务返回了不完整的音频") from None


class SpeechClient:
    def __init__(self, config, http=None):
        self.config = config
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=3), follow_redirects=False, trust_env=False)
        self.base = config["base_url"].rstrip("/")
        self.headers = {"Authorization": "Bearer " + config["api_key"]}

    async def _json(self, method, path, **kwargs):
        try:
            response = await self.http.request(method, self.base + path, headers=self.headers, follow_redirects=False, **kwargs)
            if not 200 <= response.status_code < 300:
                raise ValueError("语音服务请求失败，请检查认证、模型和服务状态")
            return response.json()
        except (httpx.HTTPError, ValueError, KeyError):
            raise ValueError("语音服务请求失败，请检查认证、模型和服务状态") from None

    async def async_probe(self):
        models = await self._json("GET", "/models")
        by_id = {m["id"]: m for m in models.get("data", [])}
        for cap in self.config["capabilities"]:
            if self.config[cap + "_model"] not in by_id:
                raise ValueError("所选模型不在语音服务的已安装模型中")
        try:
            r = await self.http.get(self.base.removesuffix("/v1") + "/health", headers=self.headers, follow_redirects=False)
            if r.status_code != 200 or r.json().get("state") != "ready":
                raise ValueError
        except (httpx.HTTPError, ValueError):
            raise ValueError("语音服务尚未就绪") from None
        tts = by_id.get(self.config.get("tts_model"), {})
        revisions = {m: by_id[m].get("revision", "") for m in by_id if m in (self.config.get("stt_model"), self.config.get("tts_model"))}
        return {"streaming": tts.get("streaming") is True, "speed_control": tts.get("speed_control") is True,
                "model_revisions": revisions}

    async def async_transcribe(self, wav, *, language):
        result = await self._json("POST", "/audio/transcriptions", data={"model": self.config["stt_model"], "language": "zh", "response_format": "json"}, files={"file": ("audio.wav", wav, "audio/wav")})
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("未识别到有效文字")
        return text.strip()

    async def async_synthesize(self, text, *, language, options):
        body = {"model": self.config["tts_model"], "input": text, "language": "zh", "voice": options.get("voice", self.config["voice"]),
                "speed": options.get("speed", self.config.get("speed", 1.0)), "response_format": "wav", "stream": False}
        data = bytearray()
        try:
            async with self.http.stream("POST", self.base + "/audio/speech", json=body, headers=self.headers, follow_redirects=False) as r:
                if r.status_code != 200 or r.headers.get("Content-Type", "").split(";")[0] not in ("audio/wav", "audio/x-wav"):
                    raise ValueError
                async for chunk in r.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_TTS_BYTES:
                        raise ValueError
        except (httpx.HTTPError, ValueError):
            raise ValueError("语音合成失败，请检查服务、音色和模型") from None
        check_wav(data)
        return "wav", bytes(data)

    async def async_stream_synthesize(self, text, *, language, options):
        if not self.config.get("verified", {}).get("streaming"):
            raise ValueError("当前服务未验证流式交付")

        async def chunks():
            total = 0
            body = {"model": self.config["tts_model"], "input": text, "voice": options.get("voice", self.config["voice"]),
                    "language": "zh", "speed": options.get("speed", 1.0), "response_format": "wav", "stream": True}
            try:
                async with self.http.stream("POST", self.base + "/audio/speech", json=body, headers=self.headers, follow_redirects=False) as r:
                    if r.status_code != 200 or r.headers.get("Content-Type", "").split(";")[0] not in ("audio/wav", "audio/x-wav"):
                        raise ValueError
                    async for chunk in r.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_TTS_BYTES:
                            raise ValueError
                        yield chunk
            except (httpx.HTTPError, ValueError):
                raise ValueError("流式语音合成失败") from None

        return "wav", chunks()

    async def async_close(self):
        await self.http.aclose()
