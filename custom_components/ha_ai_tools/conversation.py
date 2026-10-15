"""AI Tools 对话实体。"""

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
from .provider import get_provider
from .tools import exposed_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([AIToolsConversation(entry.runtime_data)])


class AIToolsConversation(ConversationEntity):
    _attr_name = "AI Tools"
    _attr_icon = "mdi:brain"
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(self, manager):
        self.manager = manager
        self.engine = ConversationEngine(manager)
        self._attr_unique_id = manager.entry.entry_id

    @property
    def supported_languages(self):
        return "*"

    async def _async_handle_message(self, user_input, chat_log):
        user_input.conversation_id = chat_log.conversation_id
        response = intent.IntentResponse(language=user_input.language)
        try:
            client, model = get_provider(self.hass, self.manager.settings["source_entity"])
            devices = [
                {"entity_id": s.entity_id, "name": s.name, "state": s.state}
                for s in exposed_entities(self.hass).values()
            ]
            text = await self.engine.async_run(
                user_input, client, model, devices, broadcast_devices(self.manager)
            )
            response.async_set_speech(text)
        except Exception as err:
            _LOGGER.error("AI Tools 对话失败: %s", type(err).__name__)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "模型服务暂时不可用，请稍后重试；已创建的项目可到AI工具页面核对。",
            )
            text = ""
        return ConversationResult(
            response=response,
            conversation_id=chat_log.conversation_id,
            continue_conversation=is_followup(text),
        )
