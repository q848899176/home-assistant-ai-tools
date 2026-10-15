"""提醒与永久记忆规则，统一使用北京时间。"""

import hashlib
import math
import re
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
REPEATS = ("none", "daily", "weekdays", "weekly")
REMINDER_HISTORY_STATES = frozenset({"completed", "cancelled", "failed", "missed"})
SENSITIVE = re.compile(
    r"密码|口令|私钥|密钥|验证码|助记词|password|passwd|api[\s_-]*key|"
    r"access[\s_-]*token|refresh[\s_-]*token|\btoken\s*[:：=]|bearer\s+|"
    r"sk-[a-z0-9_-]{12,}|-----BEGIN .*(?:PRIVATE KEY|CERTIFICATE)|"
    r"[a-z0-9_-]{20,}\.[a-z0-9_-]{20,}\.[a-z0-9_-]{10,}",
    re.IGNORECASE,
)
STABLE = re.compile(
    r"记住|记得|以后|习惯|喜欢|偏好|通常|一般|每天|叫我|我叫|家在|讨厌|称呼|默认|总是|名字|昵称|不吃|爱吃|过敏|住在|上班"
)


def now():
    return datetime.now(TZ)


def new_state():
    return {"reminders": [], "memories": [], "sessions": {}, "settings": {}, "requests": {}}


def sanitize(text):
    value = str(text)
    return "[敏感内容已省略]" if SENSITIVE.search(value) else value


def sanitize_data(value):
    if isinstance(value, dict):
        return {
            k: "[敏感内容已省略]" if SENSITIVE.search(str(k)) else sanitize_data(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    return sanitize(value) if isinstance(value, str) else value


def bounded_text(value, name, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name}不能为空且不能超过{maximum}字")
    return value.strip()


def parse_time(args, current=None):
    current = current or now()
    if ("at" in args) == ("in_minutes" in args):
        raise ValueError("请指定一个北京时间或相对分钟数")
    if "in_minutes" in args:
        minutes = args["in_minutes"]
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, (float, int))
            or not math.isfinite(minutes)
            or not 0 < minutes <= 5256000
        ):
            raise ValueError("分钟数必须大于0且不超过10年")
        result = current + timedelta(minutes=minutes)
    else:
        try:
            result = datetime.fromisoformat(args["at"])
        except (TypeError, ValueError) as err:
            raise ValueError("时间格式错误，请使用北京时间 ISO 格式") from err
        result = result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)
    if result <= current:
        raise ValueError("提醒时间必须在未来")
    return result


def next_occurrence(at, repeat, current=None):
    if repeat not in REPEATS:
        raise ValueError("不支持的重复规则")
    if repeat == "none":
        return None
    current = current or now()
    at = at.astimezone(TZ)
    step = 7 if repeat == "weekly" else 1
    days = max(0, (current.date() - at.date()).days)
    result = at + timedelta(days=(days // step) * step)
    while result <= current or (repeat == "weekdays" and result.weekday() >= 5):
        result += timedelta(days=step)
    return result


def remember(data, key, text, evidence, user_text, scope, current=None):
    key = bounded_text(key, "记忆类别", 80)
    text = bounded_text(text, "记忆内容", 1000)
    evidence = bounded_text(evidence, "原话证据", 1000)
    if any(SENSITIVE.search(value) for value in (key, text, evidence, user_text)):
        raise ValueError("敏感信息不能进入永久记忆")
    if evidence not in user_text:
        raise ValueError("记忆必须有本轮用户原话作为证据")
    if not STABLE.search(evidence):
        raise ValueError("只能自动保存明确、稳定的偏好，请使用记住指令")
    timestamp = (current or now()).isoformat()
    old = next((m for m in data["memories"] if m["scope"] == scope and m["key"] == key), None)
    record = {
        "id": old["id"] if old else uuid4().hex,
        "key": key,
        "text": text,
        "evidence": evidence,
        "scope": scope,
        "updated_at": timestamp,
    }
    if old:
        data["memories"].remove(old)
    data["memories"].append(record)
    return record


def select_memories(data, scope, query, limit=12):
    terms = {query[i : i + 2] for i in range(len(query) - 1)}
    candidates = [m for m in data["memories"] if m["scope"] in (scope, "family")]
    return sorted(
        candidates,
        key=lambda m: (sum(t in (m["key"] + m["text"]) for t in terms), m["updated_at"]),
        reverse=True,
    )[:limit]


def is_followup(text):
    return str(text).rstrip().endswith(("?", "？"))


def session_key(device_id, user_id):
    return hashlib.sha256(f"{user_id or 'family'}:{device_id or 'web'}".encode()).hexdigest()[:24]
