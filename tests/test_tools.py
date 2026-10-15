import pytest

from custom_components.ha_ai_tools.tools import validate_automation


@pytest.mark.parametrize("target", ["label_id", "floor_id"])
def test_indirect_targets_cannot_bypass_exposure(target):
    with pytest.raises(ValueError):
        validate_automation(
            {
                "actions": [
                    {
                        "action": "light.turn_off",
                        "target": {"entity_id": "light.room", target: "private"},
                    }
                ]
            },
            {"light.room"},
            lambda d, s: True,
        )


def test_scene_apply_cannot_change_hidden_entities():
    with pytest.raises(ValueError):
        validate_automation(
            {
                "actions": [
                    {"action": "scene.apply", "data": {"entities": {"light.private": "off"}}}
                ]
            },
            set(),
            lambda d, s: True,
        )


def test_event_action_cannot_invoke_privileged_automation():
    with pytest.raises(ValueError):
        validate_automation({"actions": [{"event": "privileged_event"}]}, set(), lambda d, s: True)


def test_unexposed_automation_entity_is_rejected_before_write():
    config = {
        "alias": "灯",
        "triggers": [{"trigger": "time", "at": "23:00:00"}],
        "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.private"}}],
    }
    with pytest.raises(ValueError, match="公开"):
        validate_automation(config, {"light.public"}, lambda d, s: True)


def test_privileged_action_is_rejected():
    with pytest.raises(ValueError, match="操作"):
        validate_automation(
            {"actions": [{"action": "shell_command.run"}]}, set(), lambda d, s: True
        )


def test_legal_time_automation_is_accepted():
    config = {
        "alias": "晚上关灯",
        "triggers": [{"trigger": "time", "at": "23:00:00"}],
        "actions": [{"action": "light.turn_off", "target": {"entity_id": "light.room"}}],
    }
    assert validate_automation(config, {"light.room"}, lambda d, s: True) is config


def test_area_target_cannot_bypass_entity_permissions():
    with pytest.raises(ValueError):
        validate_automation(
            {"actions": [{"action": "light.turn_off", "target": {"area_id": "all"}}]},
            set(),
            lambda d, s: True,
        )


def test_templates_cannot_bypass_entity_permissions():
    with pytest.raises(ValueError):
        validate_automation(
            {
                "actions": [
                    {
                        "action": "light.turn_off",
                        "target": {"entity_id": "{{ states('input_text.secret') }}"},
                    }
                ]
            },
            set(),
            lambda d, s: True,
        )
