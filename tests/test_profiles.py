"""助手参数隔离、验证及 HA 配置子条目。"""

from types import SimpleNamespace

import pytest

from custom_components.ha_ai_tools import provider
from tests.test_provider import clients as connection_clients  # noqa: F401
from tests.test_provider import native_manager

pytestmark = pytest.mark.usefixtures("connection_clients")


async def test_profiles_are_independent_and_default_id_stable():
    m = native_manager()
    await provider.async_save_connection(m, {"id": "one", "api_key": "one"})
    await provider.async_save_connection(m, {"id": "two", "api_key": "two"})
    await provider.async_save_profile(
        m, {"id": "default", "name": "主助手", "connection_id": "one", "chat_model": "m1"}
    )
    await provider.async_save_profile(
        m,
        {
            "id": "other",
            "name": "副助手",
            "connection_id": "two",
            "chat_model": "m2",
            "temperature": 0.1,
        },
    )
    first, primary = await provider.async_get_provider(m)
    second, other = await provider.async_get_provider(m, "other")
    assert first is not second and primary["id"] == "default"
    assert primary["chat_model"] == "m1" and other["chat_model"] == "m2"
    assert {x.subentry_type for x in m.entry.subentries.values()} == {"connection", "conversation"}
    result = provider.list_profiles(m.entry)
    result[0]["temperature"] = 99
    assert provider.get_profile(m.entry)["temperature"] != 99


@pytest.mark.parametrize(
    "updates",
    [
        {"temperature": 3},
        {"top_p": -1},
        {"max_tokens": 0},
        {"max_function_calls_per_conversation": -1},
        {"context_threshold": 0},
        {"context_truncate_strategy": "unsafe"},
        {"functions": "invalid: ["},
        {"connection_id": "missing"},
    ],
)
async def test_profile_invalid_updates_preserve_previous(updates):
    m = native_manager()
    await provider.async_save_connection(m, {"id": "one", "api_key": "one"})
    await provider.async_save_profile(m, {"id": "default", "connection_id": "one"})
    old = provider.get_profile(m.entry)
    with pytest.raises(ValueError):
        await provider.async_save_profile(m, {"id": "default", **updates})
    assert provider.get_profile(m.entry) == old


async def test_builtin_execute_services_only_accepts_compatible_native_override():
    m = native_manager()
    await provider.async_save_connection(m, {"id": "one", "api_key": "one"})
    spec = {"name": "execute_services", "parameters": {"type": "object"}}
    with pytest.raises(ValueError, match="保留"):
        await provider.async_save_profile(
            m,
            {
                "id": "default",
                "connection_id": "one",
                "functions": [
                    {"spec": spec, "function": {"type": "native", "name": "get_history"}}
                ],
            },
        )
    await provider.async_save_profile(
        m,
        {
            "id": "default",
            "connection_id": "one",
            "functions": [
                {"spec": spec, "function": {"type": "native", "name": "execute_service"}}
            ],
        },
    )


def test_options_only_fake_has_default_profile_but_no_client_dependency():
    entry = SimpleNamespace(options={"chat_model": "old-test-model"}, data={})
    assert provider.get_profile(entry)["id"] == "default"
    assert provider.get_profile(entry)["chat_model"] == "old-test-model"


def test_real_subentries_container_does_not_resurrect_deleted_default():
    entry = SimpleNamespace(options={}, data={}, subentries={})
    assert provider.list_profiles(entry) == []
    with pytest.raises(ValueError, match="不存在"):
        provider.get_profile(entry)


@pytest.mark.parametrize("directive", ["break", "continue", "do []"])
def test_prompt_accepts_homeassistant_jinja_extensions(directive):
    raw = "{% for item in [1,2] %}{% " + directive + " %}{{ item }}{% endfor %}"
    assert provider.validate_profile({"prompt": raw})["prompt"] == raw


async def test_ha_config_flow_creates_native_connection_and_default_assistant(monkeypatch):
    from custom_components.ha_ai_tools import config_flow

    monkeypatch.setattr(config_flow, "async_create_client", provider.async_create_client)

    flow = config_flow.AIToolsConfigFlow()
    flow.hass = SimpleNamespace()
    monkeypatch.setattr(flow, "async_set_unique_id", _nothing)
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda: None)
    result = await flow.async_step_user(
        {
            "name": "我的助手",
            "api_provider": "openai",
            "api_key": "test",
            "chat_model": "local",
            "outputs": [],
        }
    )
    assert result["type"] == "create_entry"
    assert "api_key" not in result["data"]
    connection, conversation = result["subentries"]
    assert connection["subentry_type"] == "connection" and connection["data"]["api_key"] == "test"
    assert conversation["data"]["id"] == "default"
    assert conversation["data"]["connection_id"] == connection["data"]["id"]


async def _nothing(*args):
    return None


async def test_options_menu_exposes_business_settings_and_import(monkeypatch):
    from custom_components.ha_ai_tools.config_flow import AIToolsOptionsFlow

    flow = AIToolsOptionsFlow()
    result = await flow.async_step_init()
    assert result["type"] == "menu"
    assert result["menu_options"] == ["settings", "import_legacy"]


async def test_connection_subentry_creates_and_reconfigures_without_secret_default(monkeypatch):
    from custom_components.ha_ai_tools import config_flow

    m = native_manager()
    monkeypatch.setattr(config_flow, "async_create_client", provider.async_create_client)
    flow = config_flow.ConnectionSubentryFlow()
    flow.hass = m.hass
    flow.context = {"source": "user"}
    monkeypatch.setattr(flow, "_get_entry", lambda: m.entry)
    result = await flow.async_step_user({"api_key": "old", "name": "连接"})
    assert result["type"] == "create_entry" and result["data"]["api_key"] == "old"
    await provider.async_save_connection(m, result["data"])
    existing = next(iter(m.entry.subentries.values()))
    flow.context = {"source": "reconfigure"}
    monkeypatch.setattr(flow, "_get_reconfigure_subentry", lambda: existing)
    form = await flow.async_step_reconfigure()
    assert "old" not in str(form["data_schema"])
    updated = await flow.async_step_reconfigure({"name": "新名称", "api_key": ""})
    assert updated["type"] == "abort" and existing.data["api_key"] == "old"


async def test_conversation_subentry_checks_connection_and_renames_default(monkeypatch):
    from custom_components.ha_ai_tools import config_flow

    m = native_manager()
    flow = config_flow.ConversationSubentryFlow()
    flow.hass = m.hass
    flow.context = {"source": "user"}
    monkeypatch.setattr(flow, "_get_entry", lambda: m.entry)
    assert (await flow.async_step_user())["reason"] == "no_connection"
    await provider.async_save_connection(m, {"id": "one", "api_key": "old"})
    created = await flow.async_step_user({"connection_id": "one", "name": "助手"})
    assert created["type"] == "create_entry" and created["data"]["id"] == "default"
    await provider.async_save_profile(m, created["data"])
    existing = next(s for s in m.entry.subentries.values() if s.subentry_type == "conversation")
    flow.context = {"source": "reconfigure"}
    monkeypatch.setattr(flow, "_get_reconfigure_subentry", lambda: existing)
    updated = await flow.async_step_reconfigure({"name": "新助手"})
    assert updated["type"] == "abort" and existing.data["name"] == "新助手"


async def test_root_reauth_updates_only_selected_connection_and_preserves_key(monkeypatch):
    from custom_components.ha_ai_tools import config_flow

    m = native_manager()
    await provider.async_save_connection(m, {"id": "one", "api_key": "old"})
    flow = config_flow.AIToolsConfigFlow()
    flow.hass = m.hass
    flow.context = {"source": "reauth", "entry_id": "root"}
    monkeypatch.setattr(flow, "_get_reauth_entry", lambda: m.entry)
    result = await flow.async_step_reauth({})
    assert result["type"] == "form" and result["step_id"] == "reauth_credentials"
    result = await flow.async_step_reauth_credentials(
        {"api_key": "", "base_url": "https://new.invalid/v1"}
    )
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert next(iter(m.entry.subentries.values())).data["api_key"] == "old"


def test_legacy_high_function_budget_is_preserved_with_engine_deadline():
    assert (
        provider.validate_profile({"max_function_calls_per_conversation": 2000})[
            "max_function_calls_per_conversation"
        ]
        == 2000
    )
