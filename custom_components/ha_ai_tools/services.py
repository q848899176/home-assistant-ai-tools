"""独立模型的图像问答与管理员连接配置服务。"""

import asyncio
import base64
import os
import stat
import warnings
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from PIL import Image, UnidentifiedImageError

DOMAIN = "ha_ai_tools"
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000

QUERY_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry"): cv.string,
        vol.Optional("profile_id"): cv.string,
        vol.Optional("model"): cv.string,
        vol.Required("prompt"): vol.All(cv.string, vol.Length(min=1, max=32000)),
        vol.Required("images"): vol.All(
            cv.ensure_list, vol.Length(min=1, max=MAX_IMAGES), [{vol.Required("url"): cv.string}]
        ),
        vol.Optional("max_tokens", default=300): vol.All(cv.positive_int, vol.Range(max=128000)),
    }
)
CHANGE_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry"): cv.string,
        vol.Optional("profile_id"): cv.string,
        vol.Optional("api_key"): cv.string,
        vol.Optional("base_url"): cv.string,
        vol.Optional("api_version"): cv.string,
        vol.Optional("organization"): cv.string,
        vol.Optional("skip_authentication"): cv.boolean,
        vol.Optional("api_provider"): cv.string,
    }
)


async def _require_admin(hass, call, *, allow_internal=False):
    """无用户身份只信任 HA 内部图像服务，不允许修改凭据。"""
    if not call.context.user_id:
        if allow_internal:
            return
        raise Unauthorized(context=call.context)
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is None or not user.is_admin or not user.is_active:
        raise Unauthorized(context=call.context)


def _manager(hass):
    if (manager := hass.data.get(DOMAIN)) is None:
        raise HomeAssistantError("AI 工具尚未加载，请先完成集成配置")
    return manager


def _target_profile(manager, entry_id, profile_id=None):
    """连接 ID 必须唯一对应助手，避免把图片发给错误的模型连接。"""
    from .provider import get_profile, list_connections, list_profiles

    if entry_id == manager.entry.entry_id:
        if profile_id is not None:
            try:
                get_profile(manager.entry, profile_id)
            except ValueError, KeyError, StopIteration:
                raise HomeAssistantError("未找到指定对话助手") from None
        return profile_id
    if entry_id not in {item["id"] for item in list_connections(manager.entry)}:
        raise HomeAssistantError("未找到指定 AI 工具配置或模型连接")
    profiles = [
        item
        for item in list_profiles(manager.entry)
        if item.get("connection_id") == entry_id
        and (profile_id is None or item["id"] == profile_id)
    ]
    if len(profiles) != 1:
        raise HomeAssistantError("模型连接无法唯一对应助手，请指定 profile_id")
    return profiles[0]["id"]


def _external_url(value):
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise HomeAssistantError("远程图片必须使用有效的 HTTP 或 HTTPS 地址")
    return value


def _read_local_image(root, value):
    """逐级 O_NOFOLLOW 打开，防止检查后替换目录或符号链接逃逸。"""
    path = Path(unquote(value))
    if ".." in path.parts or "\x00" in str(path):
        raise HomeAssistantError("图片路径不允许目录穿越")
    if str(path).startswith("/local/"):
        path = root / str(path).removeprefix("/local/")
    try:
        relative = path.relative_to(root)
        if not relative.parts:
            raise ValueError
    except ValueError:
        raise HomeAssistantError("本地图片只允许读取配置目录 www 下的文件") from None
    directory = None
    file_descriptor = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in relative.parts[:-1]:
            next_directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = next_directory
        file_descriptor = os.open(
            relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HomeAssistantError("图片必须为普通文件")
        if info.st_size > MAX_IMAGE_BYTES:
            raise HomeAssistantError("图片大小超过单张限制")
        with os.fdopen(file_descriptor, "rb") as image_file:
            file_descriptor = None
            content = image_file.read(MAX_IMAGE_BYTES + 1)
        return content
    except OSError:
        raise HomeAssistantError("本地图片不存在或路径不安全") from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory is not None:
            os.close(directory)


def _image_data_url(content):
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise HomeAssistantError("图片大小无效或超过单张限制")
    mime_types = {
        "PNG": "image/png",
        "JPEG": "image/jpeg",
        "GIF": "image/gif",
        "WEBP": "image/webp",
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                mime = mime_types.get(image.format)
                if mime is None or image.width * image.height > MAX_IMAGE_PIXELS:
                    raise HomeAssistantError("图片格式或像素数量不受支持")
                if getattr(image, "is_animated", False):
                    raise HomeAssistantError("图像问答只支持静态图片")
                image.verify()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise HomeAssistantError("图片内容无效，只支持 PNG、JPEG、GIF 或 WebP 静态图片") from None
    return "data:" + mime + ";base64," + base64.b64encode(content).decode("ascii")


async def _remote_image(hass, value):
    session = async_get_clientsession(hass)
    url = _external_url(value)
    try:
        for _ in range(4):
            async with session.get(
                url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=20)
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise HomeAssistantError("远程图片重定向无效")
                    url = _external_url(urljoin(str(response.url), location))
                    continue
                if response.status != 200:
                    raise HomeAssistantError("无法下载远程图片，请检查地址及访问权限")
                if (
                    response.content_length is not None
                    and response.content_length > MAX_IMAGE_BYTES
                ):
                    raise HomeAssistantError("图片大小超过单张限制")
                content = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    content.extend(chunk)
                    if len(content) > MAX_IMAGE_BYTES:
                        raise HomeAssistantError("图片大小超过单张限制")
                return bytes(content)
    except aiohttp.ClientError, TimeoutError, ValueError:
        raise HomeAssistantError("远程图片下载失败或超时，请检查地址") from None
    raise HomeAssistantError("远程图片重定向次数过多")


def _model_error(error):
    if getattr(error, "status_code", None) in (401, 403):
        return "模型认证失败，请由管理员检查连接凭据与权限"
    message = str(error).lower()
    if any(word in message for word in ("image", "vision", "multimodal")) and any(
        word in message for word in ("support", "invalid", "unsupported", "not allowed")
    ):
        return "当前模型不支持此图像请求，请选择支持视觉的模型并检查图片格式"
    if isinstance(error, TimeoutError):
        return "图像问答超时，请稍后重试"
    return "图像问答请求失败，请检查模型名称、视觉能力和连接状态"


async def async_setup_services(hass: HomeAssistant):
    """注册一次服务；每次请求读取当前 manager 和独立连接。"""
    if hass.services.has_service(DOMAIN, "query_image") and hass.services.has_service(
        DOMAIN, "change_config"
    ):
        return

    async def query_image(call: ServiceCall):
        from .provider import async_get_provider

        await _require_admin(hass, call, allow_internal=True)
        manager = _manager(hass)
        profile_id = _target_profile(
            manager, call.data["config_entry"], call.data.get("profile_id")
        )
        try:
            client, profile = await async_get_provider(manager, profile_id)
        except Exception:
            raise HomeAssistantError("模型连接不可用，请先完成连接和对话助手配置") from None
        images, total_bytes = [], 0
        try:
            async with asyncio.timeout(60):
                for image in call.data["images"]:
                    value = image["url"]
                    try:
                        scheme = urlsplit(value).scheme
                    except ValueError:
                        raise HomeAssistantError("图片地址无效") from None
                    if scheme:
                        content = await _remote_image(hass, value)
                    else:
                        content = await hass.async_add_executor_job(
                            _read_local_image, Path(hass.config.path("www")), value
                        )
                    total_bytes += len(content)
                    if total_bytes > MAX_TOTAL_IMAGE_BYTES:
                        raise HomeAssistantError("图片总大小超过限制")
                    url = await hass.async_add_executor_job(_image_data_url, content)
                    images.append({"type": "image_url", "image_url": {"url": url}})
        except TimeoutError:
            raise HomeAssistantError("准备图片超时，请减少图片数量或检查图片地址") from None
        model = call.data.get("model") or profile.get("chat_model")
        if not model:
            raise HomeAssistantError("请先为对话助手选择模型或通过 model 指定视觉模型")
        lower_model = model.lower()
        token_key = (
            "max_completion_tokens"
            if any(
                lower_model.startswith(prefix) or f"-{prefix}" in lower_model
                for prefix in ("gpt-4o", "gpt-5", "o1", "o3", "o4")
            )
            else "max_tokens"
        )
        try:
            async with asyncio.timeout(60):
                response = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": call.data["prompt"]}, *images],
                        }
                    ],
                    **{token_key: call.data["max_tokens"]},
                )
            result = response.model_dump(exclude_none=True)
            if not result.get("choices"):
                raise HomeAssistantError("模型未返回图像问答结果，请检查模型是否支持视觉")
            return result
        except HomeAssistantError:
            raise
        except Exception as error:
            raise HomeAssistantError(_model_error(error)) from None

    async def change_config(call: ServiceCall):
        from .provider import async_save_connection, get_profile, list_connections

        await _require_admin(hass, call)
        manager = _manager(hass)
        profile_id = call.data.get("profile_id")
        if call.data["config_entry"] == manager.entry.entry_id:
            try:
                connection_id = get_profile(manager.entry, profile_id)["connection_id"]
            except ValueError, KeyError, StopIteration:
                raise HomeAssistantError("默认助手尚未配置模型连接") from None
        else:
            connection_id = call.data["config_entry"]
            if profile_id is not None:
                _target_profile(manager, connection_id, profile_id)
        if connection_id not in {item["id"] for item in list_connections(manager.entry)}:
            raise HomeAssistantError("未找到指定模型连接")
        payload = {
            key: value
            for key, value in call.data.items()
            if key not in ("config_entry", "profile_id")
        }
        if not payload:
            return
        payload["id"] = connection_id
        try:
            await async_save_connection(manager, payload)
        except Exception:
            raise HomeAssistantError(
                "连接验证或保存失败，已保留旧配置；请检查连接参数和凭据"
            ) from None

    hass.services.async_register(
        DOMAIN,
        "query_image",
        query_image,
        schema=QUERY_IMAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "change_config",
        change_config,
        schema=CHANGE_CONFIG_SCHEMA,
    )
