"""独立连接的凭据、原子更新与迁移边界。"""

from copy import deepcopy
from types import MappingProxyType, SimpleNamespace

import pytest

from custom_components.ha_ai_tools import provider


class Entries:
    def __init__(self, entry):
        self.entries = {entry.entry_id: entry}
        self.fail_save = False

    def async_get_entry(self, entry_id):
        return self.entries.get(entry_id)

    def async_add_subentry(self, entry, subentry):
        if self.fail_save:
            raise OSError("synthetic persistence failure")
        entry.subentries[subentry.subentry_id] = subentry
        return True

    def async_update_subentry(self, entry, subentry, **kwargs):
        if self.fail_save:
            raise OSError("synthetic persistence failure")
        for key, value in kwargs.items():
            object.__setattr__(subentry, key, MappingProxyType(value) if key == "data" else value)
        return True

    def async_remove_subentry(self, entry, subentry_id):
        if self.fail_save:
            raise OSError("synthetic persistence failure")
        del entry.subentries[subentry_id]
        return True


class Client:
    def __init__(self):
        self.closed = False
        self.chat = object()
        self.models = SimpleNamespace(list=self.list_models)

    async def list_models(self, **kwargs):
        return []

    async def close(self):
        self.closed = True


def native_manager():
    entry = SimpleNamespace(
        entry_id="root", title="AI 家庭助手", subentries={}, options={}, data={}
    )
    entries = Entries(entry)
    return SimpleNamespace(entry=entry, hass=SimpleNamespace(config_entries=entries))


@pytest.fixture
def clients(monkeypatch):
    created = []

    async def create(hass, data, *, validate=False):
        if data.get("api_key") == "invalid":
            raise ValueError("连接认证失败")
        client = Client()
        created.append((deepcopy(data), validate, client))
        return client

    monkeypatch.setattr(provider, "async_create_client", create, raising=False)
    return created


async def test_connection_credentials_only_in_subentry_and_runtime_is_independent(clients):
    m = native_manager()
    await provider.async_save_connection(
        m, {"id": "one", "name": "本地", "api_key": "test-only-key"}
    )
    await provider.async_save_profile(
        m, {"id": "default", "connection_id": "one", "chat_model": "local"}
    )
    m.entry.runtime_data = object()
    safe = provider.list_connections(m.entry)
    assert "test-only-key" not in str(safe)
    assert safe[0]["api_key_configured"] is True
    client, profile = await provider.async_get_provider(m)
    assert client is clients[-1][2] and profile["chat_model"] == "local"
    assert not m.entry.data and not m.entry.options
    assert all(x[0].get("api_key") == "test-only-key" for x in clients)


async def test_invalid_or_unpersisted_update_preserves_connection_and_cached_client(clients):
    m = native_manager()
    await provider.async_save_connection(m, {"id": "one", "api_key": "old"})
    await provider.async_save_profile(m, {"id": "default", "connection_id": "one"})
    old, _ = await provider.async_get_provider(m)
    before = deepcopy(
        dict(next(s for s in m.entry.subentries.values() if s.subentry_type == "connection").data)
    )
    with pytest.raises(ValueError):
        await provider.async_save_connection(m, {"id": "one", "api_key": "invalid"})
    m.hass.config_entries.fail_save = True
    with pytest.raises(OSError):
        await provider.async_save_connection(m, {"id": "one", "api_key": "valid-new"})
    current, _ = await provider.async_get_provider(m)
    assert current is old and not old.closed
    assert (
        dict(next(s for s in m.entry.subentries.values() if s.subentry_type == "connection").data)
        == before
    )
    assert clients[-1][2].closed


async def test_blank_key_keeps_secret_explicit_clear_requires_skip_auth(clients):
    m = native_manager()
    await provider.async_save_connection(m, {"id": "one", "api_key": "old"})
    await provider.async_save_connection(m, {"id": "one", "api_key": "", "name": "new"})
    assert clients[-1][0]["api_key"] == "old"
    with pytest.raises(ValueError):
        await provider.async_save_connection(m, {"id": "one", "clear_api_key": True})
    await provider.async_save_connection(
        m, {"id": "one", "clear_api_key": True, "skip_authentication": True}
    )
    assert not provider.list_connections(m.entry)[0]["api_key_configured"]


async def test_cannot_delete_referenced_connection_and_unload_closes_clients(clients):
    m = native_manager()
    await provider.async_save_connection(m, {"id": "one", "api_key": "old"})
    await provider.async_save_profile(m, {"id": "default", "connection_id": "one"})
    client, _ = await provider.async_get_provider(m)
    with pytest.raises(ValueError, match="引用"):
        await provider.async_save_connection(m, {"id": "one", "delete": True})
    await provider.async_close_providers(m)
    assert client.closed


async def test_legacy_import_copies_all_profiles_once_without_runtime(monkeypatch, clients):
    from homeassistant.helpers import entity_registry as er

    m = native_manager()
    old = SimpleNamespace(
        entry_id="legacy",
        domain="extended_openai_conversation",
        title="旧模型",
        data={"api_key": "legacy-key", "base_url": "https://model.invalid/v1"},
        options={},
        subentries={
            "a": SimpleNamespace(
                subentry_id="a",
                subentry_type="conversation",
                title="主助手",
                data={
                    "prompt": "原样 {{ ha_name }}",
                    "chat_model": "legacy-model",
                    "functions": "[]",
                    "temperature": 0.7,
                },
            ),
            "b": SimpleNamespace(
                subentry_id="b",
                subentry_type="conversation",
                title="副助手",
                data={"prompt": "另一个", "chat_model": "other", "functions": "[]"},
            ),
        },
    )
    m.hass.config_entries.entries["legacy"] = old
    monkeypatch.setattr(
        er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get=lambda entity: SimpleNamespace(
                config_entry_id="legacy", config_subentry_id="a"
            )
        ),
    )
    first = await provider.async_import_legacy(m, "conversation.legacy")
    second = await provider.async_import_legacy(m, "conversation.legacy")
    assert first["saved"] and second["saved"]
    assert len(provider.list_connections(m.entry)) == 1
    assert len(provider.list_profiles(m.entry)) == 2
    profile = provider.get_profile(m.entry)
    assert profile["prompt"] == "原样 {{ ha_name }}" and profile["temperature"] == 0.7
    assert "legacy-key" not in str(first)
    assert (await provider.async_get_provider(m))[0] is not getattr(old, "runtime_data", None)


async def test_legacy_invalid_functions_leaves_existing_configuration(monkeypatch, clients):
    from homeassistant.helpers import entity_registry as er

    m = native_manager()
    old = SimpleNamespace(
        entry_id="legacy",
        domain="extended_openai_conversation",
        title="旧模型",
        data={"api_key": "old"},
        options={"functions": "not: [valid"},
        subentries={},
    )
    m.hass.config_entries.entries["legacy"] = old
    monkeypatch.setattr(
        er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get=lambda entity: SimpleNamespace(
                config_entry_id="legacy", config_subentry_id=None
            )
        ),
    )
    with pytest.raises(ValueError):
        await provider.async_import_legacy(m, "conversation.legacy")
    assert not m.entry.subentries


async def test_sdk_request_without_auth_uses_own_client(monkeypatch):
    import httpx

    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"object": "list", "data": []})

    monkeypatch.setattr(
        provider,
        "_http_client",
        lambda skip: httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            auth=provider._NoAuthentication() if skip else None,
        ),
        raising=False,
    )
    client = await provider.async_create_client(
        None,
        {
            "api_provider": "openai",
            "base_url": "https://local.invalid/v1",
            "api_key": "",
            "skip_authentication": True,
        },
        validate=True,
    )
    await client.models.list()
    await client.close()
    assert requests and "authorization" not in requests[0].headers
    assert "api-key" not in requests[0].headers


async def test_sdk_azure_endpoint_version_and_secret_errors_are_safe(monkeypatch):
    import httpx

    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(
            401, json={"error": {"message": "SECRET_MUST_NOT_APPEAR", "type": "invalid_api_key"}}
        )

    monkeypatch.setattr(
        provider,
        "_http_client",
        lambda skip: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(ValueError, match="认证失败") as error:
        await provider.async_create_client(
            None,
            {
                "api_provider": "azure",
                "base_url": "https://azure.invalid",
                "api_version": "2024-10-21",
                "api_key": "test-key",
            },
            validate=True,
        )
    assert "SECRET_MUST_NOT_APPEAR" not in str(error.value)
    assert requests[0].url.params["api-version"] == "2024-10-21"
    assert requests[0].headers.get("api-key") == "test-key"


async def test_import_partial_persistence_failure_removes_candidates(monkeypatch, clients):
    from homeassistant.helpers import entity_registry as er

    m = native_manager()
    old = SimpleNamespace(
        entry_id="legacy",
        domain="extended_openai_conversation",
        title="旧模型",
        data={"api_key": "old"},
        options={"functions": "[]"},
        subentries={},
    )
    m.hass.config_entries.entries["legacy"] = old
    monkeypatch.setattr(
        er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get=lambda entity: SimpleNamespace(
                config_entry_id="legacy", config_subentry_id=None
            )
        ),
    )
    original = m.hass.config_entries.async_add_subentry

    def fail_profile(entry, subentry):
        if subentry.subentry_type == "conversation":
            raise OSError("synthetic save failure")
        return original(entry, subentry)

    monkeypatch.setattr(m.hass.config_entries, "async_add_subentry", fail_profile)
    with pytest.raises(OSError):
        await provider.async_import_legacy(m, "conversation.legacy")
    assert not m.entry.subentries and clients[-1][2].closed


async def test_saved_connection_can_be_tested_before_any_profile_exists(clients):
    m = native_manager()
    await provider.async_save_connection(m, {"id": "one", "api_key": "test"})
    result = await provider.async_test_connection(m, connection_id="one")
    assert result["connected"] and clients[-1][2].closed
    assert "test" not in str(result)
    assert provider.list_profiles(m.entry) == []


async def test_connection_test_response_cannot_echo_server_error(monkeypatch, clients):
    m = native_manager()
    await provider.async_save_connection(m, {"id": "one", "api_key": "test"})

    async def fail(self, **kwargs):
        raise ValueError("SECRET_SERVER_ERROR")

    monkeypatch.setattr(Client, "list_models", fail)
    with pytest.raises(ValueError) as error:
        await provider.async_test_connection(m, connection_id="one")
    assert "SECRET_SERVER_ERROR" not in str(error.value)
    assert clients[-1][2].closed


async def test_http_client_creation_runs_outside_event_loop(monkeypatch):
    import threading

    import httpx

    event_loop_thread = threading.get_ident()
    created_threads = []

    def create(skip):
        created_threads.append(threading.get_ident())
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    monkeypatch.setattr(provider, "_http_client", create)
    client = await provider.async_create_client(None, {"api_key": "test"})
    await client.close()
    assert created_threads and created_threads[0] != event_loop_thread


async def test_owned_http_client_uses_homeassistant_ca_context(monkeypatch):
    import ssl

    import httpx
    from homeassistant.util.ssl import SSL_ALPN_HTTP11, client_context

    original = httpx.AsyncClient
    seen = []

    def create(**kwargs):
        seen.append(kwargs)
        return original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200)), **kwargs
        )

    monkeypatch.setattr(httpx, "AsyncClient", create)
    client = provider._http_client(False)
    await client.aclose()
    assert seen and isinstance(seen[0].get("verify"), ssl.SSLContext)
    assert seen[0]["verify"] is client_context(alpn_protocols=SSL_ALPN_HTTP11)
