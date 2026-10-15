"""复用现有提供方，不复制或显示密钥。"""


def get_provider(hass, source_entity):
    from homeassistant.helpers import entity_registry as er

    entity = er.async_get(hass).async_get(source_entity)
    if entity is None or not entity.config_entry_id:
        raise ValueError("请选择已配置的 Extended OpenAI 对话来源")
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    if entry is None or entry.domain != "extended_openai_conversation":
        raise ValueError("当前支持复用 Extended OpenAI Conversation 的模型配置")
    client = getattr(entry, "runtime_data", None)
    if client is None or not hasattr(client, "chat"):
        raise ValueError("模型来源未加载，请先检查现有 DeepSeek 集成")
    subentry = entry.subentries.get(entity.config_subentry_id)
    options = subentry.data if subentry else entry.options
    return client, options.get("chat_model", "deepseek-chat")
