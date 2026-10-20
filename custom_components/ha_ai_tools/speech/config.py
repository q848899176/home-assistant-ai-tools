"""语音连接子条目；不复制凭据到业务存储或页面。"""

import hashlib
import json
from copy import deepcopy
from urllib.parse import urlsplit

from ..models import bounded_text, now
from ..provider import _new_id, _save_subentry

DEFAULTS = {"name": "本地语音", "base_url": "", "capabilities": ["stt", "tts"],
            "stt_model": "", "stt_language": "zh", "tts_model": "", "tts_language": "zh",
            "voice": "Serena", "speed": 1.0}


def raw_speech(entry):
    return [{**deepcopy(dict(s.data)), "id": s.data.get("id", s.subentry_id), "subentry_id": s.subentry_id}
            for s in getattr(entry, "subentries", {}).values() if s.subentry_type == "speech"]


def list_speech(entry):
    return [{k: deepcopy(v) for k, v in s.items() if k in {*DEFAULTS, "id", "subentry_id", "config_revision", "verified"}}
            | {"api_key_configured": bool(s.get("api_key"))} for s in raw_speech(entry)]


def validate_speech(payload, previous=None):
    if not isinstance(payload, dict):
        raise ValueError("语音配置必须是对象")
    previous = previous or {}
    value = {**deepcopy(DEFAULTS), **{k: previous[k] for k in {*DEFAULTS, "api_key", "id"} if k in previous}}
    value.update({k: payload[k] for k in DEFAULTS if k in payload})
    value["id"] = _new_id(payload.get("id", previous.get("id")))
    value["name"] = bounded_text(value["name"], "语音名称", 80)
    parsed = urlsplit(value["base_url"])
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("请填写不含凭据的HTTP或HTTPS语音服务地址")
    value["base_url"] = value["base_url"].rstrip("/")
    if not value["base_url"].endswith("/v1"):
        value["base_url"] += "/v1"
    if payload.get("clear_api_key"):
        value["api_key"] = ""
    elif payload.get("api_key"):
        value["api_key"] = bounded_text(payload["api_key"], "服务密钥", 1000)
    if not value.get("api_key"):
        raise ValueError("请配置独立语音服务密钥")
    caps = value["capabilities"]
    if not isinstance(caps, list) or not caps or any(c not in ("stt", "tts") for c in caps):
        raise ValueError("请选择识别或合成能力")
    value["capabilities"] = sorted(set(caps))
    for capability in caps:
        value[capability + "_model"] = bounded_text(value[capability + "_model"], "语音模型", 256)
        if value[capability + "_language"] not in ("zh", "zh-CN", "zh_CN"):
            raise ValueError("当前已验证语言为中文")
    if "tts" in caps:
        value["voice"] = bounded_text(value["voice"], "音色", 100)
    if isinstance(value["speed"], bool) or not isinstance(value["speed"], (float, int)) or not 0.5 <= value["speed"] <= 2:
        raise ValueError("语速必须在0.5至2之间")
    value["config_revision"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]
    return value


async def async_save_speech(manager, payload):
    from .client import SpeechClient

    previous = next((s for s in raw_speech(manager.entry) if s["id"] == payload.get("id")), None)
    value = validate_speech(payload, previous)
    client = SpeechClient(value)
    try:
        verified = await client.async_probe()
    finally:
        await client.async_close()
    value["verified"] = {**verified, "tested_at": now().isoformat()}
    _save_subentry(manager, "speech", value["id"], value)
    return {"id": value["id"], "saved": True}
