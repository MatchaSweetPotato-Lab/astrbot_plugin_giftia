from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from core.database.data_cache import DataCache
from core.handlers.commands import CommandHandler


@pytest.fixture
def command():
    plugin = SimpleNamespace(
        adapter_id_map={"adapter": "bot"},
        embedding_conf={"enabled": False},
        data_cache=SimpleNamespace(delete_all_memories=AsyncMock()),
    )
    event = SimpleNamespace(
        platform_meta=SimpleNamespace(id="adapter"),
        get_group_id=Mock(return_value="group"),
        get_sender_id=Mock(return_value="sender"),
        send=AsyncMock(),
    )
    return CommandHandler(plugin), plugin, event


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["group", ""])
async def test_clear_memory_uses_current_conversation_without_embeddings(
    command, group_id
):
    handler, plugin, event = command
    event.get_group_id.return_value = group_id

    _ = [chunk async for chunk in handler.delete_all_memories(event)]

    plugin.data_cache.delete_all_memories.assert_awaited_once_with(
        bot_name="bot", group_or_user_id=group_id or "sender"
    )
    assert "当前会话" in event.send.call_args.args[0].chain[0].text
    assert "成功" in event.send.call_args.args[0].chain[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["bot", "session"])
async def test_clear_memory_rejects_unresolved_scope(command, missing):
    handler, plugin, event = command
    if missing == "bot":
        plugin.adapter_id_map.clear()
    else:
        event.get_group_id.return_value = ""
        event.get_sender_id.return_value = ""

    _ = [chunk async for chunk in handler.delete_all_memories(event)]

    plugin.data_cache.delete_all_memories.assert_not_awaited()
    assert "成功" not in event.send.call_args.args[0].chain[0].text


@pytest.mark.asyncio
async def test_clear_memory_reports_failure_without_success(command):
    handler, plugin, event = command
    plugin.data_cache.delete_all_memories.side_effect = RuntimeError(
        "database unavailable"
    )

    _ = [chunk async for chunk in handler.delete_all_memories(event)]

    event.send.assert_awaited_once()
    assert "失败" in event.send.call_args.args[0].chain[0].text
    assert "成功" not in event.send.call_args.args[0].chain[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("embedding_enabled", [False, True])
async def test_clear_memory_preserves_unrelated_data_and_caches(embedding_enabled):
    db = SimpleNamespace(delete_all_memories=AsyncMock())
    ltm = SimpleNamespace(
        embedding_conf={"enabled": embedding_enabled},
        delete_all_memories=AsyncMock(return_value=True),
    )
    cache = DataCache(db, None, ltm)
    for store in (
        cache.group_profiles,
        cache.bot_status,
        cache.recent_messages,
        cache.user_profiles,
        cache.user_profile_records,
        cache.relations,
    ):
        store["bot:group"] = "preserved"
        store["bot:other-group"] = "also preserved"
    cache.memories["bot:group"].append("removed")
    cache.memories["bot:other-group"].append("kept")

    await cache.delete_all_memories("bot", "group")

    db.delete_all_memories.assert_awaited_once_with(
        bot_name="bot", group_or_user_id="group"
    )
    assert "bot:group" not in cache.memories
    assert list(cache.memories["bot:other-group"]) == ["kept"]
    for store in (
        cache.group_profiles,
        cache.bot_status,
        cache.recent_messages,
        cache.user_profiles,
        cache.user_profile_records,
        cache.relations,
    ):
        assert store == {
            "bot:group": "preserved",
            "bot:other-group": "also preserved",
        }
    if embedding_enabled:
        ltm.delete_all_memories.assert_awaited_once_with(
            bot_name="bot", group_or_user_id="group"
        )
    else:
        ltm.delete_all_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_memory_database_error_preserves_memory_caches():
    db = SimpleNamespace(
        delete_all_memories=AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    cache = DataCache(db, None, None)
    cache.memories["bot:group"].append("kept")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await cache.delete_all_memories("bot", "group")

    assert list(cache.memories["bot:group"]) == ["kept"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, RuntimeError("vector store unavailable")])
async def test_clear_memory_vector_failure_is_reported_after_cache_invalidation(result):
    db = SimpleNamespace(delete_all_memories=AsyncMock())
    delete_vectors = AsyncMock(return_value=result)
    if isinstance(result, Exception):
        delete_vectors.side_effect = result
    cache = DataCache(
        db,
        None,
        SimpleNamespace(
            embedding_conf={"enabled": True}, delete_all_memories=delete_vectors
        ),
    )
    cache.memories["bot:group"].append("removed")

    with pytest.raises(RuntimeError):
        await cache.delete_all_memories("bot", "group")

    db.delete_all_memories.assert_awaited_once()
    assert "bot:group" not in cache.memories
