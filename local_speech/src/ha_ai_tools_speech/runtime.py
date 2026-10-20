"""串行推理及 Apple MLX 运行时；加载仅允许已安装目录。"""

import asyncio
import io
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager


class ServiceBusy(Exception):
    pass


class InferenceGate:
    def __init__(self, wait_timeout=5):
        self.lock = asyncio.Lock()
        self.waiting = 0
        self.wait_timeout = wait_timeout

    @asynccontextmanager
    async def slot(self):
        if self.lock.locked() and self.waiting >= 4:
            raise ServiceBusy
        self.waiting += 1
        acquired = False
        try:
            try:
                async with asyncio.timeout(self.wait_timeout):
                    await self.lock.acquire()
                    acquired = True
            except TimeoutError:
                raise ServiceBusy from None
        finally:
            self.waiting -= 1
        try:
            yield
        finally:
            if acquired:
                self.lock.release()

    async def run(self, operation):
        async with self.slot():
            return await operation()


class MLXRuntime:
    def __init__(self, settings):
        self.settings = settings
        self.models = {}
        self.state = "loading"
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-speech")
        self.cancel = threading.Event()

    def status(self):
        return {"state": self.state, "models": list(self.models)}

    async def _work(self, function, *args):
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self.worker, function, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.cancel.set()
            # Native GPU work cannot be interrupted safely mid-kernel. Keep the
            # single worker occupied until its next result, then discard it.
            await asyncio.shield(future)
            raise

    async def async_start(self):
        def load():
            import os

            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            for identifier, entry in self.settings.models.items():
                if "stt" in entry["capabilities"]:
                    from mlx_audio.stt.utils import load_model
                else:
                    from mlx_audio.tts.utils import load_model
                self.models[identifier] = load_model(entry["path"])

        try:
            await self._work(load)
            self.state = "ready"
        except Exception:
            self.state = "error"
            raise

    async def async_close(self):
        self.cancel.set()
        await asyncio.to_thread(self.worker.shutdown, wait=True, cancel_futures=True)

    async def async_transcribe(self, audio, *, model, language):
        def generate():
            import numpy as np
            import soundfile as sf

            samples, rate = sf.read(io.BytesIO(audio), dtype="float32")
            if rate != 16000:
                raise ValueError("识别仅接收16kHz音频")
            result = self.models[model].generate(np.asarray(samples), language="Chinese")
            return result.text.strip()

        self.cancel.clear()
        return await self._work(generate)

    async def async_synthesize(self, text, *, model, language, options, stream):
        import numpy as np

        self.cancel.clear()
        generator = self.models[model].generate(
            text=text, voice=options["voice"], lang_code="Chinese", stream=False,
        )
        buffers, sample_rate = [], None
        while not self.cancel.is_set():
            result = await self._work(lambda: next(generator, None))
            if result is None:
                break
            rate = int(result.sample_rate)
            if sample_rate is not None and rate != sample_rate:
                raise ValueError("音频采样率发生变化")
            sample_rate = rate
            audio = np.asarray(result.audio).reshape(-1)
            buffers.append((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
        if not buffers:
            raise ValueError("合成未生成音频")
        output = io.BytesIO()
        with wave.open(output, "wb") as f:
            f.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
            f.writeframes(b"".join(buffers))
        yield output.getvalue()
