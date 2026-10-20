import asyncio
import io
import json
import wave

import httpx
import pytest


def wav():
    b = io.BytesIO()
    with wave.open(b, "wb") as f:
        f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        f.writeframes(b"\0\0" * 1600)
    return b.getvalue()


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.download_calls = []

    def status(self):
        return {"state": "ready"}

    async def async_transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return "下午三点提醒喝水"

    async def async_synthesize(self, text, **kwargs):
        self.calls.append(kwargs)
        yield wav()


@pytest.fixture
def server(tmp_path):
    from ha_ai_tools_speech.app import create_app
    from ha_ai_tools_speech.config import Settings

    rt = FakeRuntime()
    settings = Settings(
        bind_host="127.0.0.1", port=18973, api_key="PRIVATE_SENTINEL_FOR_TESTING_ONLY",
        allowed_sources=["127.0.0.1"],
        models={"asr": {"path": str(tmp_path), "revision": "test", "capabilities": ["stt"]},
                "tts": {"path": str(tmp_path), "revision": "test", "capabilities": ["tts"]}},
    )
    return create_app(settings, rt), rt


async def test_unauthenticated_before_audio_read(server):
    app, rt = server
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/audio/transcriptions", content=b"invalid")
        assert r.status_code == 401
        assert rt.calls == []


async def test_foreign_source_denied(server):
    app, rt = server
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.8", 1)), base_url="http://test") as c:
        assert (await c.get("/health", headers={"Authorization": "Bearer PRIVATE_SENTINEL_FOR_TESTING_ONLY"})).status_code == 403
        assert rt.calls == []


async def test_routes_models_and_fields_are_restricted(server):
    app, rt = server
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer PRIVATE_SENTINEL_FOR_TESTING_ONLY"}) as c:
        assert (await c.post("/v1/models")).status_code == 405
        assert (await c.post("/v1/audio/speech", json={"model": "tts", "input": "你好", "voice": "Serena", "ref_audio": "/private/file"})).status_code == 422
        assert (await c.post("/v1/audio/speech", json={"model": "unknown", "input": "你好", "voice": "Serena"})).status_code == 400
        assert rt.calls == rt.download_calls == []


async def test_valid_audio_endpoints_and_no_download_for_voices(server):
    app, rt = server
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer PRIVATE_SENTINEL_FOR_TESTING_ONLY"}) as c:
        r = await c.post("/v1/audio/transcriptions", data={"model": "asr", "language": "zh", "response_format": "json"}, files={"file": ("audio.wav", wav(), "audio/wav")})
        assert r.json() == {"text": "下午三点提醒喝水"}
        r = await c.post("/v1/audio/speech", json={"model": "tts", "input": "你好", "voice": "Serena"})
        assert r.status_code == 200
        assert r.content == wav()
        voices = (await c.get("/v1/audio/voices", params={"model": "tts"})).json()["data"]
        assert {v["id"] for v in voices} >= {"Vivian", "Serena", "Uncle_Fu", "Dylan"}
        assert rt.download_calls == []
        assert rt.calls[0]["model"] == "asr"


async def test_wav_is_not_header_only(server):
    app, rt = server
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer PRIVATE_SENTINEL_FOR_TESTING_ONLY"}) as c:
        r = await c.post("/v1/audio/transcriptions", data={"model": "asr"}, files={"file": ("audio.wav", wav()[:44], "audio/wav")})
        assert r.status_code == 400
        assert rt.calls == []


async def test_chunked_upload_is_bounded_without_content_length(server):
    app, rt = server

    async def body():
        yield b'--bound\r\nContent-Disposition: form-data; name="file"; filename="x.wav"\r\nContent-Type: audio/wav\r\n\r\n'
        for _ in range(22):
            yield b"x" * (1024 * 1024)
        yield b"\r\n--bound--\r\n"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer PRIVATE_SENTINEL_FOR_TESTING_ONLY"}) as c:
        r = await c.post("/v1/audio/transcriptions", content=body(), headers={"Content-Type": "multipart/form-data; boundary=bound"})
        assert r.status_code == 413
        assert rt.calls == []


@pytest.mark.parametrize("endpoint", ["speech", "transcriptions"])
@pytest.mark.parametrize("queued", [False, True])
async def test_http_disconnect_cancels_inference_or_queued_request(server, endpoint, queued):
    app, runtime = server
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def slow(*args, **kwargs):
        calls.append(1)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "text"

    async def speech(*args, **kwargs):
        await slow()
        yield wav()

    runtime.async_transcribe = slow
    runtime.async_synthesize = speech
    if endpoint == "speech":
        body = json.dumps({"model": "tts", "input": "你好", "voice": "Serena"}).encode()
        content_type = "application/json"
    else:
        request = httpx.Request("POST", "http://test", data={"model": "asr"}, files={"file": ("audio.wav", wav(), "audio/wav")})
        body = request.read()
        content_type = request.headers["Content-Type"]
    messages = asyncio.Queue()
    messages.put_nowait({"type": "http.request", "body": body, "more_body": False})
    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/v1/audio/" + endpoint, "raw_path": ("/v1/audio/" + endpoint).encode(),
             "query_string": b"", "root_path": "", "server": ("test", 80), "client": ("127.0.0.1", 1000),
             "headers": [(b"authorization", b"Bearer PRIVATE_SENTINEL_FOR_TESTING_ONLY"),
                         (b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())]}
    task = asyncio.create_task(app(scope, messages.get, send))
    waiting = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        if queued:
            waiting_messages = asyncio.Queue()
            waiting_messages.put_nowait({"type": "http.request", "body": body, "more_body": False})
            waiting = asyncio.create_task(app(scope, waiting_messages.get, send))
            await asyncio.sleep(.02)
            waiting_messages.put_nowait({"type": "http.disconnect"})
        else:
            messages.put_nowait({"type": "http.disconnect"})
        await asyncio.sleep(.05)
        if queued:
            await asyncio.wait_for(waiting, 1)
            assert not cancelled.is_set() and len(calls) == 1
        else:
            assert cancelled.is_set(), "HTTP disconnect must cancel inference before normal completion"
            await asyncio.wait_for(task, 1)
    finally:
        release.set()
        task.cancel()
        if waiting:
            waiting.cancel()
        await asyncio.gather(*[t for t in (task, waiting) if t], return_exceptions=True)
