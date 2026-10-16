"""AI Tools 对话实体。"""

import asyncio
import logging

from homeassistant.components.conversation import (
    ConversationEntity,
    ConversationEntityFeature,
    ConversationResult,
)
from homeassistant.helpers import intent

from .api import broadcast_devices
from .engine import ConversationEngine
from .models import is_followup
from .provider import async_get_provider, list_profiles
from .tools import exposed_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    profiles = list_profiles(entry)
    if not profiles and getattr(entry.runtime_data, "migration_error", None):
        profiles = [{"id": "default", "name": "AI Tools"}]
    for profile in profiles:
        options = (
            {"config_subentry_id": profile["subentry_id"]} if profile.get("subentry_id") else {}
        )
        async_add_entities([AIToolsConversation(entry.runtime_data, profile)], **options)


class AIToolsConversation(ConversationEntity):
    _attr_name = "AI Tools"
    _attr_icon = "mdi:brain"
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, manager, profile=None):
        self.manager = manager
        self.engine = ConversationEngine(manager)
        self.profile_id = (profile or {}).get("id", "default")
        self._attr_name = (profile or {}).get("name", "AI Tools")
        self._attr_unique_id = manager.entry.entry_id + (
            "_" + self.profile_id if self.profile_id != "default" else ""
        )

    @property
    def supported_languages(self):
        return "*"

    async def _async_handle_message(self, user_input, chat_log):
        user_input.conversation_id = chat_log.conversation_id
        response = intent.IntentResponse(language=user_input.language)
        task = asyncio.current_task()
        self.manager.active_tasks.add(task)
        try:
            from homeassistant.auth.permissions.const import POLICY_READ
            from homeassistant.helpers import entity_registry as er

            client, profile = await async_get_provider(self.manager, self.profile_id)
            registry = er.async_get(self.hass)
            actor = (
                await self.hass.auth.async_get_user(user_input.context.user_id)
                if user_input.context.user_id
                else None
            )
            if user_input.context.user_id and (actor is None or not actor.is_active):
                raise ValueError("当前用户身份无效")
            devices = [
                {
                    "entity_id": s.entity_id,
                    "name": s.name,
                    "state": s.state,
                    "aliases": er.async_get_entity_aliases(
                        self.hass, registry.async_get(s.entity_id)
                    )
                    if registry.async_get(s.entity_id)
                    else [],
                }
                for s in exposed_entities(self.hass).values()
                if actor is None or actor.permissions.check_entity(s.entity_id, POLICY_READ)
            ]
            text = await self.engine.async_run(
                user_input,
                client,
                profile["chat_model"],
                devices,
                [
                    d
                    for d in broadcast_devices(self.manager)
                    if actor is None or actor.permissions.check_entity(d["entity_id"], POLICY_READ)
                ],
                profile=profile,
            )
            response.async_set_speech(text)
        except Exception as err:
            _LOGGER.error("AI Tools 对话失败: %s", type(err).__name__)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "模型服务暂时不可用，请稍后重试；已创建的项目可到AI工具页面核对。",
            )
            text = ""
        finally:
            self.manager.active_tasks.discard(task)
        return ConversationResult(
            response=response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=is_followup(text),
        )
