from types import SimpleNamespace

from custom_components.ha_ai_tools import conversation


async def test_profiles_register_to_subentries_and_default_preserves_entity_id(monkeypatch):
    profiles = [
        {"id": "default", "name": "AI Tools", "subentry_id": "profile-1"},
        {"id": "bedroom", "name": "卧室助手", "subentry_id": "profile-2"},
    ]
    manager = SimpleNamespace(entry=SimpleNamespace(entry_id="original"))
    entry = SimpleNamespace(runtime_data=manager)
    monkeypatch.setattr(conversation, "list_profiles", lambda _: profiles, raising=False)
    added = []
    await conversation.async_setup_entry(
        None, entry, lambda entities, **kw: added.append((entities, kw))
    )
    assert len(added) == 2
    assert added[0][0][0].unique_id == "original"
    assert added[1][0][0].unique_id == "original_bedroom"
    assert added[1][1]["config_subentry_id"] == "profile-2"
