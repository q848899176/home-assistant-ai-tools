"""分开记录音频首段、完整读取及冷/热请求，报告不含正文。"""

import argparse
import io
import json
import math
import statistics
import time
import unicodedata
import wave
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx


def audio_stats(data, *, partial=False):
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            expected = audio.getnframes()
            width = audio.getsampwidth() * audio.getnchannels()
            pcm = audio.readframes(expected)
            frames = len(pcm) // width
            valid = frames > 0 and (partial or frames == expected)
            return {"valid_audio": valid, "duration_seconds": frames / audio.getframerate()}
    except (wave.Error, EOFError, ValueError, ZeroDivisionError):
        return {"valid_audio": False, "duration_seconds": 0}


def summarize(rows):
    result = {}
    for name, selected in (
        ("cold", [r for r in rows if r.get("cold")]),
        ("cached", [r for r in rows if r.get("cached") and not r.get("cold")]),
        ("warm_uncached", [r for r in rows if not r.get("cached") and not r.get("cold")]),
    ):
        values = sorted(r["elapsed_ms"] for r in selected if "elapsed_ms" in r)
        result[name] = {"count": len(values), "p50_ms": statistics.median(values) if values else None,
                        "p95_ms": values[max(0, math.ceil(len(values) * .95) - 1)] if values else None}
    return result


def character_error(reference, hypothesis):
    def normalize(s):
        return "".join(c.lower() for c in s if unicodedata.category(c)[0] not in "PZ")

    a, b = normalize(reference), normalize(hypothesis)
    row = list(range(len(b) + 1))
    for i, c in enumerate(a, 1):
        current = [i]
        for j, d in enumerate(b, 1):
            current.append(min(current[-1] + 1, row[j] + 1, row[j - 1] + (c != d)))
        row = current
    return {"errors": row[-1], "characters": len(a)}


def measure_speech(endpoint, *, credentials, corpus: Path, output: Path):
    samples = json.loads(corpus.read_text())
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    audio_dir = output.parent / "samples"
    audio_dir.mkdir(exist_ok=True, mode=0o700)
    tts_rows, stt_rows = [], []
    with httpx.Client(base_url=endpoint.rstrip("/"), headers={"Authorization": "Bearer " + credentials["api_key"]}, timeout=45, trust_env=False) as client:
        for i, sample in enumerate(samples.get("tts", [])):
            started = time.perf_counter()
            first = None
            data = bytearray()
            with client.stream("POST", "/v1/audio/speech", json={"model": credentials["tts_model"], "input": sample["text"], "voice": sample.get("voice", "Serena"), "language": "zh", "response_format": "wav"}) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes():
                    data.extend(chunk)
                    if first is None and audio_stats(data, partial=True)["valid_audio"]:
                        first = (time.perf_counter() - started) * 1000
            elapsed = (time.perf_counter() - started) * 1000
            stats = audio_stats(data)
            if not stats["valid_audio"]:
                raise ValueError("音频损坏，不能记录为有效性能结果")
            target = audio_dir / f"tts-{i:03d}.wav"
            target.write_bytes(data)
            target.chmod(0o600)
            tts_rows.append({"characters": len(sample["text"]), "voice": sample.get("voice", "Serena"),
                             "cold": False, "cached": False, "first_decodable_ms": first,
                             "elapsed_ms": elapsed, "audio_seconds": stats["duration_seconds"],
                             "rtf": elapsed / 1000 / stats["duration_seconds"], "sample": target.name})
        for sample in samples.get("stt", []):
            path = corpus.parent / sample["file"]
            started = time.perf_counter()
            r = client.post("/v1/audio/transcriptions", data={"model": credentials["stt_model"], "language": "zh", "response_format": "json"}, files={"file": ("audio.wav", path.read_bytes(), "audio/wav")})
            r.raise_for_status()
            elapsed = (time.perf_counter() - started) * 1000
            stt_rows.append({"elapsed_ms": elapsed, "cold": False, "cached": False,
                             **character_error(sample["reference"], r.json()["text"])})
    result = {"at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "tts": tts_rows,
              "stt": stt_rows, "tts_summary": summarize(tts_rows), "stt_summary": summarize(stt_rows),
              "tts_first_audio": summarize([{**r, "elapsed_ms": r["first_decodable_ms"]} for r in tts_rows]),
              "measurement": "API音频测量，不代表Nabu实际首声；常驻预热，未启用服务缓存"}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    output.chmod(0o600)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from .config import load_settings

    cfg = load_settings(args.config)
    credentials = {"api_key": cfg.api_key}
    for capability in ("stt", "tts"):
        credentials[capability + "_model"] = next((m for m, v in cfg.models.items() if capability in v["capabilities"]), "")
    r = measure_speech(f"http://{cfg.bind_host}:{cfg.port}", credentials=credentials, corpus=args.corpus, output=args.output)
    print(json.dumps({k: r[k] for k in ("tts_summary", "tts_first_audio", "stt_summary")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
