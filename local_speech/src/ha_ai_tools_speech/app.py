"""受认证与模型白名单保护的最小音频 API。"""

import asyncio
import hmac
import io
import ipaddress
import wave
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

from .runtime import InferenceGate, ServiceBusy

MAX_AUDIO = 20 * 1024 * 1024
VOICES = ["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee"]


class RequestLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        count = 0

        async def bounded_receive():
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > MAX_AUDIO + 65536:
                raise HTTPException(413, "音频超过大小限制")
            return message

        await self.app(scope, bounded_receive, send)


def validate_wav(data):
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            frames = audio.getnframes()
            pcm = audio.readframes(frames)
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                    or audio.getframerate() != 16000 or frames == 0
                    or frames > 16000 * 60 or len(pcm) != frames * 2):
                raise ValueError
    except (ValueError, EOFError, wave.Error):
        raise HTTPException(400, "需要完整的单声道16kHz、16bit WAV，最长60秒") from None


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str
    input: str = Field(min_length=1, max_length=1000)
    voice: str
    language: str = "zh"
    speed: float = 1.0
    response_format: str = "wav"
    stream: bool = False


async def until_disconnected(request, operation):
    """Cancel queued/active work when its HTTP consumer disappears."""
    async def disconnect():
        while (await request.receive())["type"] != "http.disconnect":
            pass

    task = asyncio.create_task(operation)
    watcher = asyncio.create_task(disconnect())
    try:
        await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher.done():
            raise HTTPException(499, "请求已取消")
        return await task
    finally:
        task.cancel()
        watcher.cancel()
        # MLX waits for an in-flight native kernel and discards its result.
        # Do not release the single inference slot until that cleanup finishes.
        await asyncio.shield(asyncio.gather(task, watcher, return_exceptions=True))


def create_app(settings, runtime):
    gate = InferenceGate()

    @asynccontextmanager
    async def lifespan(app):
        if hasattr(runtime, "async_start"):
            await runtime.async_start()
        try:
            yield
        finally:
            if hasattr(runtime, "async_close"):
                await runtime.async_close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestLimitMiddleware)

    @app.middleware("http")
    async def protection(request, call_next):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + settings.api_key):
            return JSONResponse({"detail": "语音服务认证失败"}, status_code=401)
        peer = ipaddress.ip_address(request.client.host)
        if not any(peer in ipaddress.ip_network(s, strict=False) for s in settings.allowed_sources):
            return JSONResponse({"detail": "访问来源未允许"}, status_code=403)
        try:
            if int(request.headers.get("content-length", 0)) > MAX_AUDIO + 65536:
                return JSONResponse({"detail": "音频超过大小限制"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "请求大小无效"}, status_code=400)
        try:
            return await call_next(request)
        except ServiceBusy:
            return JSONResponse({"detail": "语音服务忙"}, status_code=503)
        except TimeoutError:
            return JSONResponse({"detail": "语音推理超时"}, status_code=504)

    def check_model(model, capability):
        if model not in settings.models or capability not in settings.models[model]["capabilities"]:
            raise HTTPException(400, "该模型未安装或不支持此能力")

    @app.get("/health")
    async def health():
        status = runtime.status()
        return {**status, "queue_waiting": gate.waiting, "busy": gate.lock.locked()}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": m, "object": "model", "revision": v["revision"],
                                              "capabilities": v["capabilities"], "streaming": False,
                                              "speed_control": False}
                                             for m, v in settings.models.items()]}

    @app.get("/v1/audio/voices")
    async def voices(model: str):
        check_model(model, "tts")
        return {"data": [{"id": v, "name": v} for v in VOICES]}

    @app.post("/v1/audio/speech")
    async def speech(body: SpeechRequest, request: Request):
        check_model(body.model, "tts")
        if body.voice not in VOICES or body.speed != 1.0 or body.language not in ("zh", "zh-CN", "Chinese"):
            raise HTTPException(400, "音色、语速或语言不支持")
        if body.response_format != "wav" or body.stream:
            raise HTTPException(400, "当前已验证的交付方式为完整WAV")
        async def generate():
            async with gate.slot():
                async with asyncio.timeout(30):
                    try:
                        return [c async for c in runtime.async_synthesize(
                            body.input, model=body.model, language=body.language,
                            options={"voice": body.voice, "speed": 1.0}, stream=False)]
                    except (ValueError, RuntimeError):
                        raise HTTPException(502, "语音合成失败") from None
        chunks = await until_disconnected(request, generate())
        if not chunks:
            raise HTTPException(502, "未生成音频")
        return Response(b"".join(chunks), media_type="audio/wav")

    @app.post("/v1/audio/transcriptions")
    async def transcribe(request: Request):
        form = await request.form(max_files=1, max_fields=5, max_part_size=MAX_AUDIO)
        try:
            if set(form) - {"file", "model", "language", "response_format"}:
                raise HTTPException(422, "识别参数不支持")
            model = form.get("model", "")
            check_model(model, "stt")
            if form.get("language", "zh") not in ("zh", "zh-CN", "Chinese"):
                raise HTTPException(400, "识别语言不支持")
            upload = form.get("file")
            if not hasattr(upload, "read"):
                raise HTTPException(400, "请上传音频")
            data = await upload.read(MAX_AUDIO + 1)
            if len(data) > MAX_AUDIO:
                raise HTTPException(413, "音频超过大小限制")
            validate_wav(data)
            async def generate():
                async with gate.slot():
                    async with asyncio.timeout(30):
                        try:
                            return await runtime.async_transcribe(data, model=model, language="zh")
                        except (ValueError, RuntimeError):
                            raise HTTPException(502, "语音识别失败") from None
            text = await until_disconnected(request, generate())
            if not text.strip():
                raise HTTPException(422, "未识别到文字")
            return {"text": text.strip()}
        finally:
            await form.close()

    return app
