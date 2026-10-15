from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.ha_ai_tools import models

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 16, 20, 0, tzinfo=TZ)


def test_relative_reminder_uses_beijing_time():
    assert models.parse_time({"in_minutes": 90}, NOW).isoformat() == "2026-10-16T21:30:00+08:00"


@pytest.mark.parametrize(
    "args",
    [
        {"in_minutes": 0},
        {"in_minutes": -1},
        {"at": "昨天"},
        {"at": "2026-10-16T19:00:00+08:00"},
        {"at": "2026-10-17T10:00:00+08:00", "in_minutes": 5},
    ],
)
def test_invalid_reminder_does_not_schedule(args):
    with pytest.raises(ValueError):
        models.parse_time(args, NOW)


def test_weekday_reminder_skips_weekend():
    assert models.next_occurrence(NOW, "weekdays", NOW).isoformat() == "2026-10-19T20:00:00+08:00"


def test_weekly_reminder_skips_expired_occurrences():
    old = datetime(2026, 9, 25, 9, 0, tzinfo=TZ)
    assert models.next_occurrence(old, "weekly", NOW).isoformat() == "2026-10-23T09:00:00+08:00"


def test_once_reminder_has_no_next_occurrence():
    assert models.next_occurrence(NOW, "none", NOW) is None


def test_memory_requires_evidence_from_current_user():
    data = models.new_state()
    with pytest.raises(ValueError, match="原话"):
        models.remember(data, "temperature", "喜欢 26 度", "我喜欢 26 度", "请开灯", "family", NOW)
    assert data["memories"] == []


@pytest.mark.parametrize(
    "text",
    [
        "记住密码是 abc123",
        "我的 API key 是 hello",
        "sk-abcdefghijklmnopqrstuvwxyz",
        "私钥 -----BEGIN PRIVATE KEY-----",
        "我的门锁密码是123456",
        "token: secret",
    ],
)
def test_secrets_are_rejected_and_not_persisted(text):
    data = models.new_state()
    with pytest.raises(ValueError, match="敏感"):
        models.remember(data, "secret", text, text, text, "family", NOW)
    assert models.sanitize(text) == "[敏感内容已省略]"
    assert data["memories"] == []


def test_memory_update_replaces_old_preference_with_evidence():
    data = models.new_state()
    models.remember(data, "temperature", "喜欢26度", "我喜欢26度", "我喜欢26度", "family", NOW)
    models.remember(
        data, "temperature", "喜欢25度", "以后我喜欢25度", "以后我喜欢25度", "family", NOW
    )
    assert len(data["memories"]) == 1
    assert data["memories"][0]["text"] == "喜欢25度"
    assert data["memories"][0]["evidence"] == "以后我喜欢25度"


def test_memory_scope_does_not_leak_between_users():
    data = models.new_state()
    models.remember(data, "name", "叫甲", "记住我叫甲", "记住我叫甲", "user:a", NOW)
    models.remember(data, "place", "家在北京", "记住家在北京", "记住家在北京", "family", NOW)
    result = models.select_memories(data, "user:b", "我叫什么名字", limit=10)
    assert [item["key"] for item in result] == ["place"]


def test_temporary_state_is_not_automatically_remembered():
    data = models.new_state()
    with pytest.raises(ValueError, match="稳定"):
        models.remember(data, "light", "灯开着", "灯开着", "灯开着", "family", NOW)


def test_followup_recognizes_chinese_question_marks():
    assert models.is_followup("你希望在哪个房间播报？")
    assert not models.is_followup("已设置明天上午九点提醒。")


def test_session_scope_uses_device_and_confirmed_user():
    assert models.session_key("device-a", None) != models.session_key("device-b", None)
    assert models.session_key("device-a", "a") != models.session_key("device-a", "b")
