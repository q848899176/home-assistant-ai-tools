"""原生服务通过真实 HA 注册、权限和图片边界验证。"""

import base64
import importlib
from io import BytesIO
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from PIL import Image
from probatio import Invalid as SchemaInvalid

from custom_components.ha_ai_tools import provider


def png_bytes():
    output = BytesIO()
    Image.new("RGB", (2, 2), (20, 80, 120)).save(output, "PNG")
    return output.getvalue()


@pytest.fixture
async def setup_services(tmp_path, monkeypatch):
    services = importlib.import_module("custom_components.ha_ai_tools.services")
    hass = HomeAssistant(str(tmp_path))
    users = {
        "admin": SimpleNamespace(is_admin=True, is_active=True),
        "regular": SimpleNamespace(is_admin=False, is_active=True),
        "inactive": SimpleNamespace(is_admin=True, is_active=False),
    }

    async def get_user(user_id):
        return users.get(user_id)

    hass.auth = SimpleNamespace(async_get_user=get_user)
    manager = SimpleNamespace(hass=hass, entry=SimpleNamespace(entry_id="root", subentries={}))
    profiles = [
        {"id": "default", "connection_id": "first", "chat_model": "vision-model"},
        {"id": "other", "connection_id": "second", "chat_model": "other-model"},
    ]
    connections = [{"id": "first"}, {"id": "second"}]
    requests, selections, writes = [], [], []
    response = {
        "id": "test-response",
        "object": "chat.completion",
        "created": 0,
        "model": "vision-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "logprobs": None,
                "message": {"role": "assistant", "content": "图片中是蓝色方块。", "refusal": None},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }

    async def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(model_dump=lambda **kwargs: response)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def get_provider(current, profile_id=None):
        assert current is manager
        selections.append(profile_id)
        return client, next(p for p in profiles if p["id"] == (profile_id or "default"))

    def get_profile(entry, profile_id=None):
        return next(p for p in profiles if p["id"] == (profile_id or "default"))

    async def save_connection(current, payload):
        assert current is manager
        writes.append(dict(payload))
        return {"id": payload["id"], "saved": True}

    monkeypatch.setattr(provider, "list_profiles", lambda entry: profiles, raising=False)
    monkeypatch.setattr(provider, "get_profile", get_profile, raising=False)
    monkeypatch.setattr(provider, "list_connections", lambda entry: connections, raising=False)
    monkeypatch.setattr(provider, "async_get_provider", get_provider, raising=False)
    monkeypatch.setattr(provider, "async_save_connection", save_connection, raising=False)
    session = aiohttp.ClientSession()
    monkeypatch.setattr(services, "async_get_clientsession", lambda hass: session)
    hass.data["ha_ai_tools"] = manager
    await services.async_setup_services(hass)
    yield SimpleNamespace(
        hass=hass,
        manager=manager,
        requests=requests,
        selections=selections,
        writes=writes,
        profiles=profiles,
        client=client,
        services=services,
    )
    await session.close()


async def call_service(setup, service, data, user="admin", response=True):
    return await setup.hass.services.async_call(
        "ha_ai_tools",
        service,
        data,
        blocking=True,
        context=Context(user_id=user),
        return_response=response,
    )


async def test_query_image_encodes_local_file_and_returns_response(setup_services, tmp_path):
    setup = setup_services
    image = tmp_path / "www" / "scene.png"
    image.parent.mkdir()
    image.write_bytes(png_bytes())
    result = await call_service(
        setup,
        "query_image",
        {
            "config_entry": "root",
            "prompt": "看到了什么？",
            "images": [{"url": str(image)}],
            "model": "local-vision",
            "max_tokens": 42,
        },
    )
    assert result["choices"][0]["message"]["content"] == "图片中是蓝色方块。"
    request = setup.requests[0]
    assert request["model"] == "local-vision"
    assert request["max_tokens"] == 42
    assert request["messages"][0]["content"] == [
        {"type": "text", "text": "看到了什么？"},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(png_bytes()).decode("ascii")
            },
        },
    ]


async def test_query_image_fetches_remote_image_and_checks_actual_type(setup_services):
    setup = setup_services
    app = web.Application()

    async def photo(request):
        return web.Response(body=png_bytes(), content_type="image/png")

    app.router.add_get("/photo.jpg", photo)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        await call_service(
            setup,
            "query_image",
            {
                "config_entry": "second",
                "prompt": "描述",
                "images": [{"url": f"http://127.0.0.1:{port}/photo.jpg"}],
            },
        )
    finally:
        await runner.cleanup()
    request = setup.requests[0]
    assert setup.selections == ["other"]
    assert request["model"] == "other-model"
    assert request["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


@pytest.mark.parametrize("user", ["regular", "missing", "inactive"])
async def test_query_image_rejects_unauthorized_user_before_reading(setup_services, user):
    with pytest.raises(Unauthorized):
        await call_service(
            setup_services,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": "/secret.png"}],
            },
            user=user,
        )
    assert setup_services.requests == []


async def test_query_image_trusted_internal_context_uses_default_profile(setup_services, tmp_path):
    image = tmp_path / "www" / "scene.png"
    image.parent.mkdir()
    image.write_bytes(png_bytes())
    await call_service(
        setup_services,
        "query_image",
        {
            "config_entry": "root",
            "prompt": "描述",
            "images": [{"url": str(image)}],
        },
        user=None,
    )
    assert setup_services.selections == [None]


@pytest.mark.parametrize("path", ["configuration.yaml", ".storage/auth", "www/../secret.png"])
async def test_query_image_refuses_configuration_and_traversal(setup_services, tmp_path, path):
    image = tmp_path / path
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(png_bytes())
    with pytest.raises(HomeAssistantError):
        await call_service(
            setup_services,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": str(image)}],
            },
        )
    assert setup_services.requests == []


async def test_query_image_refuses_symlink_escape(setup_services, tmp_path):
    outside = tmp_path / "secret.png"
    outside.write_bytes(png_bytes())
    image = tmp_path / "www" / "link.png"
    image.parent.mkdir()
    image.symlink_to(outside)
    with pytest.raises(HomeAssistantError):
        await call_service(
            setup_services,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": str(image)}],
            },
        )
    assert setup_services.requests == []


@pytest.mark.parametrize("content", [b"not an image", b"<svg xmlns='http://www.w3.org/2000/svg'/>"])
async def test_query_image_refuses_disguised_or_unsupported_files(
    setup_services, tmp_path, content
):
    image = tmp_path / "www" / "scene.png"
    image.parent.mkdir()
    image.write_bytes(content)
    with pytest.raises(HomeAssistantError, match="图片"):
        await call_service(
            setup_services,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": str(image)}],
            },
        )
    assert setup_services.requests == []


async def test_query_image_rejects_too_many_or_large_images(setup_services, tmp_path, monkeypatch):
    setup = setup_services
    image = tmp_path / "www" / "scene.png"
    image.parent.mkdir()
    image.write_bytes(png_bytes())
    monkeypatch.setattr(setup.services, "MAX_IMAGE_BYTES", 20)
    with pytest.raises(HomeAssistantError, match="大小"):
        await call_service(
            setup,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": str(image)}],
            },
        )
    with pytest.raises((HomeAssistantError, SchemaInvalid)):
        await call_service(
            setup,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": str(image)}] * 11,
            },
        )
    assert setup.requests == []


async def test_query_image_refuses_unknown_or_ambiguous_connection(setup_services):
    setup = setup_services
    setup.profiles.append({"id": "duplicate", "connection_id": "first", "chat_model": "m"})
    for entry_id in ("unknown", "first"):
        with pytest.raises(HomeAssistantError):
            await call_service(
                setup,
                "query_image",
                {
                    "config_entry": entry_id,
                    "prompt": "描述",
                    "images": [{"url": "/missing.png"}],
                },
            )
    assert setup.selections == []


async def test_query_image_uses_profile_and_new_token_parameter(setup_services, tmp_path):
    image = tmp_path / "www" / "scene.png"
    image.parent.mkdir()
    image.write_bytes(png_bytes())
    await call_service(
        setup_services,
        "query_image",
        {
            "config_entry": "second",
            "profile_id": "other",
            "model": "gpt-5-mini",
            "prompt": "描述",
            "images": [{"url": str(image)}],
            "max_tokens": 50,
        },
    )
    assert setup_services.requests[0]["max_completion_tokens"] == 50
    assert "max_tokens" not in setup_services.requests[0]


async def test_query_image_vision_error_is_clear_and_redacts_provider_error(
    setup_services, tmp_path
):
    async def fail(**kwargs):
        raise RuntimeError("model does not support image_url; token=SECRET")

    setup_services.client.chat.completions.create = fail
    image = tmp_path / "www" / "scene.png"
    image.parent.mkdir()
    image.write_bytes(png_bytes())
    with pytest.raises(HomeAssistantError, match="不支持.*图像") as error:
        await call_service(
            setup_services,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": str(image)}],
            },
        )
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("user", [None, "regular", "missing", "inactive"])
async def test_change_config_requires_identified_active_admin(setup_services, user):
    with pytest.raises(Unauthorized):
        await call_service(
            setup_services,
            "change_config",
            {
                "config_entry": "root",
                "api_key": "replacement",
            },
            user=user,
            response=False,
        )
    assert setup_services.writes == []


async def test_change_config_maps_root_and_preserves_empty_key(setup_services):
    await call_service(
        setup_services,
        "change_config",
        {
            "config_entry": "root",
            "api_key": "",
            "base_url": "https://example.test/v1",
            "api_version": "2024-10-21",
            "organization": "org-test",
            "api_provider": "azure",
            "skip_authentication": False,
        },
        response=False,
    )
    assert setup_services.writes == [
        {
            "id": "first",
            "api_key": "",
            "base_url": "https://example.test/v1",
            "api_version": "2024-10-21",
            "organization": "org-test",
            "api_provider": "azure",
            "skip_authentication": False,
        }
    ]


async def test_change_config_failure_keeps_old_configuration_and_hides_secret(
    setup_services, monkeypatch
):
    entry = setup_services.manager.entry
    entry.data = {"business": "keep"}
    entry.options = {"outputs": ["assist_satellite.test"]}

    async def fail(manager, payload):
        raise ValueError("invalid key: SECRET")

    monkeypatch.setattr(provider, "async_save_connection", fail)
    with pytest.raises(HomeAssistantError, match="旧配置") as error:
        await call_service(
            setup_services,
            "change_config",
            {
                "config_entry": "second",
                "api_key": "SECRET",
            },
            response=False,
        )
    assert "SECRET" not in str(error.value)
    assert entry.data == {"business": "keep"}
    assert entry.options == {"outputs": ["assist_satellite.test"]}


async def test_registration_is_idempotent_and_missing_manager_is_clear(setup_services):
    setup = setup_services
    await setup.services.async_setup_services(setup.hass)
    setup.hass.data.pop("ha_ai_tools")
    with pytest.raises(HomeAssistantError, match="尚未加载"):
        await call_service(
            setup,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": "/missing.png"}],
            },
        )


async def test_change_config_direct_connection_does_not_require_unique_profile(setup_services):
    setup_services.profiles.append(
        {
            "id": "duplicate",
            "connection_id": "first",
            "chat_model": "different-model",
        }
    )
    await call_service(
        setup_services,
        "change_config",
        {
            "config_entry": "first",
            "organization": "updated-org",
        },
        response=False,
    )
    assert setup_services.writes == [{"id": "first", "organization": "updated-org"}]


async def test_query_image_rejects_remote_oversize_without_content_length(
    setup_services,
    monkeypatch,
):
    setup = setup_services
    monkeypatch.setattr(setup.services, "MAX_IMAGE_BYTES", 20)

    async def too_large(request):
        response = web.StreamResponse(headers={"Content-Type": "image/png"})
        await response.prepare(request)
        await response.write(png_bytes())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/large", too_large)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(HomeAssistantError, match="大小"):
            await call_service(
                setup,
                "query_image",
                {
                    "config_entry": "root",
                    "prompt": "描述",
                    "images": [{"url": f"http://127.0.0.1:{port}/large"}],
                },
            )
    finally:
        await runner.cleanup()
    assert setup.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///config/www/image.png",
        "ftp://example.test/image.png",
        "https://user:password@example.test/image.png",
        "https://example.test:invalid/image.png",
    ],
)
async def test_query_image_rejects_unsafe_remote_url(setup_services, url):
    with pytest.raises(HomeAssistantError, match="地址"):
        await call_service(
            setup_services,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": url}],
            },
        )
    assert setup_services.requests == []


async def test_query_image_rejects_symlink_directory_and_encoded_traversal(
    setup_services,
    tmp_path,
):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "image.png").write_bytes(png_bytes())
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    for url in [str(tmp_path / "www" / "escape" / "image.png"), "/local/%2e%2e/outside/image.png"]:
        with pytest.raises(HomeAssistantError):
            await call_service(
                setup_services,
                "query_image",
                {
                    "config_entry": "root",
                    "prompt": "描述",
                    "images": [{"url": url}],
                },
            )
    assert setup_services.requests == []


async def test_query_image_local_alias_and_multiple_images(setup_services, tmp_path):
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "image.png").write_bytes(png_bytes())
    await call_service(
        setup_services,
        "query_image",
        {
            "config_entry": "root",
            "prompt": "比较",
            "images": [
                {"url": "/local/image.png"},
                {"url": "/local/image.png"},
            ],
        },
    )
    assert len(setup_services.requests[0]["messages"][0]["content"]) == 3


@pytest.mark.parametrize("target", ["/image", "file:///config/configuration.yaml"])
async def test_query_image_validates_every_redirect(setup_services, target):
    async def redirect(request):
        raise web.HTTPFound(target)

    async def image(request):
        return web.Response(body=png_bytes(), content_type="image/png")

    app = web.Application()
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/image", image)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    data = {
        "config_entry": "root",
        "prompt": "描述",
        "images": [{"url": f"http://127.0.0.1:{port}/redirect"}],
    }
    try:
        if target.startswith("/"):
            result = await call_service(setup_services, "query_image", data)
            assert result["choices"][0]["message"]["content"] == "图片中是蓝色方块。"
        else:
            with pytest.raises(HomeAssistantError, match="地址"):
                await call_service(setup_services, "query_image", data)
            assert setup_services.requests == []
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("url", ["https://[broken/image.png", "/local/image%00.png"])
async def test_query_image_malformed_urls_return_safe_chinese_error(setup_services, tmp_path, url):
    (tmp_path / "www").mkdir()
    with pytest.raises(HomeAssistantError, match="图片.*(地址|路径)"):
        await call_service(
            setup_services,
            "query_image",
            {
                "config_entry": "root",
                "prompt": "描述",
                "images": [{"url": url}],
            },
        )
    assert setup_services.requests == []


@pytest.mark.parametrize("status", [200, 400, 401])
async def test_query_image_actual_sdk_response_and_errors(
    setup_services, tmp_path, monkeypatch, status
):
    import json

    import httpx
    from openai import AsyncOpenAI

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if status != 200:
            body = {
                "error": {
                    "message": "model does not support image_url; SECRET",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "unsupported_image",
                }
            }
        else:
            body = {
                "id": "sdk-test",
                "object": "chat.completion",
                "created": 0,
                "model": "vision",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "真正的 SDK 结果",
                        },
                    }
                ],
            }
        return httpx.Response(status, json=body)

    transport = httpx.MockTransport(respond)
    client = AsyncOpenAI(
        api_key="test-only",
        base_url="https://example.test/v1",
        http_client=httpx.AsyncClient(transport=transport),
        max_retries=0,
    )

    async def get_provider(manager, profile_id=None):
        return client, {"id": "default", "chat_model": "vision"}

    monkeypatch.setattr(provider, "async_get_provider", get_provider)
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "sdk.png").write_bytes(png_bytes())
    data = {"config_entry": "root", "prompt": "描述", "images": [{"url": "/local/sdk.png"}]}
    try:
        if status == 200:
            result = await call_service(setup_services, "query_image", data)
            assert result["choices"][0]["message"]["content"] == "真正的 SDK 结果"
        else:
            with pytest.raises(HomeAssistantError) as error:
                await call_service(setup_services, "query_image", data)
            assert "SECRET" not in str(error.value)
            if status == 401:
                assert "认证" in str(error.value)
            else:
                assert "不支持" in str(error.value)
    finally:
        await client.close()
    assert requests[0]["model"] == "vision"
    assert requests[0]["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
