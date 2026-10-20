"""有界的PCM与WAV处理，不在事件循环进行重采样。"""

import asyncio
import io
import re
import wave

MAX_PCM = min(20 * 1024 * 1024, 16000 * 2 * 60)


async def async_collect_pcm(stream, metadata):
    from homeassistant.components import stt

    if (metadata.language not in ("zh", "zh-CN", "zh_CN") or metadata.format != stt.AudioFormats.WAV
            or metadata.codec != stt.AudioCodecs.PCM or metadata.sample_rate != 16000
            or metadata.bit_rate != 16 or metadata.channel != 1):
        raise ValueError("识别音频格式不支持")
    data = bytearray()
    iterator = aiter(stream)
    try:
        async for chunk in iterator:
            data.extend(chunk)
            if len(data) > MAX_PCM:
                raise ValueError("识别音频超过60秒或大小限制")
    finally:
        if hasattr(iterator, "aclose"):
            await iterator.aclose()
    if not data or len(data) % 2:
        raise ValueError("音频为空或采样不完整")
    return bytes(data)


def pcm_to_wav(pcm, *, sample_rate, sample_width, channels):
    output = io.BytesIO()
    with wave.open(output, "wb") as f:
        f.setparams((channels, sample_width, sample_rate, 0, "NONE", "not compressed"))
        f.writeframes(pcm)
    return output.getvalue()


def split_text(text, limit=1000):
    chunks, pending = [], ""
    for segment in re.findall(r"[^。！？!?\n]*[。！？!?\n]|[^。！？!?\n]+$", text):
        if len(pending) + len(segment) > limit and pending:
            chunks.append(pending)
            pending = ""
        while len(segment) > limit:
            chunks.append(segment[:limit])
            segment = segment[limit:]
        pending += segment
    if pending:
        chunks.append(pending)
    return chunks


def merge_wavs(values):
    parameters, pcm = None, bytearray()
    for data in values:
        with wave.open(io.BytesIO(data), "rb") as f:
            current = (f.getframerate(), f.getsampwidth(), f.getnchannels())
            if parameters and parameters != current:
                raise ValueError("合成音频格式不一致")
            parameters = current
            count = f.getnframes()
            frames = f.readframes(count)
            if not count or len(frames) != count * current[1] * current[2]:
                raise ValueError("合成音频不完整")
            pcm.extend(frames)
            if len(pcm) > 32 * 1024 * 1024:
                raise ValueError("合成音频超过大小限制")
    if parameters is None:
        raise ValueError("合成音频为空")
    return pcm_to_wav(pcm, sample_rate=parameters[0], sample_width=parameters[1], channels=parameters[2])


async def async_prepare_upload(hass, data, content_type):
    if not data or len(data) > 20 * 1024 * 1024:
        raise ValueError("上传音频为空或过大")
    is_wav = content_type.split(";")[0] in ("audio/wav", "audio/x-wav", "audio/wave")
    if is_wav:
        try:
            with wave.open(io.BytesIO(data), "rb") as f:
                count = f.getnframes()
                if not 0 < count / f.getframerate() <= 60:
                    raise ValueError("音频时长无效")
                frames = f.readframes(count)
                if len(frames) != count * f.getsampwidth() * f.getnchannels():
                    raise ValueError("音频不完整")
                if f.getframerate() == 16000 and f.getsampwidth() == 2 and f.getnchannels() == 1:
                    return data
        except (wave.Error, EOFError):
            raise ValueError("WAV格式无效") from None
    elif content_type.split(";")[0] not in ("audio/webm", "video/webm", "audio/ogg", "audio/mpeg", "audio/mp4"):
        raise ValueError("上传音频格式不支持")
    from homeassistant.components.ffmpeg import get_ffmpeg_manager

    binary = get_ffmpeg_manager(hass).binary
    process = await asyncio.create_subprocess_exec(binary, "-hide_banner", "-loglevel", "error", "-nostdin", "-protocol_whitelist", "pipe",
                                                   "-i", "pipe:0", "-t", "60.001", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", "-f", "wav", "pipe:1",
                                                   stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(30):
            output, _ = await process.communicate(data)
        if process.returncode:
            raise ValueError("音频转换失败")
        with wave.open(io.BytesIO(output), "rb") as f:
            pcm = f.readframes(960017)
        if not pcm or len(pcm) > MAX_PCM:
            raise ValueError("音频超过60秒或为空")
        return pcm_to_wav(pcm, sample_rate=16000, sample_width=2, channels=1)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
