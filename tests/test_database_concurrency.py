import asyncio
import sqlite3
from contextlib import asynccontextmanager

import aiosqlite
import pytest
import pytest_asyncio
from core.database.database import Database
from core.utils.schemas import MessageData


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_type", ["repository", "profile", "query"])
async def test_memory_writer_waits_until_shared_reader_closes(
    database, monkeypatch, reader_type
):
    db = database
    await db.upsert_kv_data("seed", "value")
    await db.upsert_group_profile("group", "bot", "value")
    reading, release = asyncio.Event(), asyncio.Event()
    original_execute = db.conn.execute
    reader = writer = None
    overlap = False

    @asynccontextmanager
    async def paused_execute(sql, parameters=()):
        nonlocal overlap
        if asyncio.current_task() is writer and not release.is_set():
            overlap = True
        async with original_execute(sql, parameters) as cursor:
            if asyncio.current_task() is reader:
                reading.set()
                await release.wait()
            yield cursor

    async def read_value():
        if reader_type == "repository":
            return await db.get_kv_data("seed")
        if reader_type == "profile":
            return await db.get_group_profile("group", "bot")
        async with db.query("SELECT value FROM kv_store WHERE key='seed'") as cursor:
            return (await cursor.fetchone())[0]

    with monkeypatch.context() as patch:
        patch.setattr(db.conn, "execute", paused_execute)
        reader = asyncio.create_task(read_value())
        try:
            await asyncio.wait_for(reading.wait(), timeout=2)
            writer = asyncio.create_task(
                db.memories_repo.insert_memories(
                    "bot", "group", [], batch_key="committed-memory-batch"
                )
            )
            # Let the writer reach its first database await while the read is held.
            await asyncio.sleep(0)
        finally:
            release.set()
            results = await asyncio.wait_for(
                asyncio.gather(reader, *([writer] if writer else [])), timeout=2
            )

    assert results == ["value", []]
    assert not overlap
    assert await db.get_kv_data("committed-memory-batch") == 1
    await db.upsert_kv_data("after-memory", 1)
    assert not db.conn.in_transaction


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["kv", "token_usage"])
async def test_stale_snapshot_failure_does_not_poison_later_writes(database, operation):
    db = database
    await db.upsert_kv_data("seed-1", 1)
    await db.upsert_kv_data("seed-2", 2)
    reader = await db.conn.execute("SELECT * FROM kv_store ORDER BY key")
    try:
        await reader.fetchone()
        async with aiosqlite.connect(db.db_path) as writer:
            await writer.execute(
                "INSERT INTO kv_store (key, value) VALUES ('external-write', 1)"
            )
            await writer.commit()
        if operation == "kv":
            with pytest.raises(aiosqlite.OperationalError) as failure:
                await db.upsert_kv_data("failed-write", 1)
            assert failure.value.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        else:
            await db.log_token_usage(
                "bot", "group", "test", "provider", "model", 1, 1, 2
            )
        assert not db.conn.in_transaction
    finally:
        await reader.close()

    await db.upsert_kv_data("recovered-write", 1)
    await db.insert_message(
        "bot", MessageData(group_or_user_id="group", message_id="recovered-message")
    )
    assert await db.get_kv_data("recovered-write") == 1
    assert await db.get_kv_data("failed-write") is None
    assert await db.get_max_message_id("bot", "group") > 0
    assert not db.conn.in_transaction


@pytest.mark.asyncio
async def test_failed_profile_write_cannot_be_committed_by_another_store(database):
    db = database
    await db.conn.execute(
        "CREATE TRIGGER reject_profile_session BEFORE INSERT ON user_profile_sessions "
        "BEGIN SELECT RAISE(ABORT, 'profile link failure'); END"
    )
    await db.conn.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="profile link failure"):
        await db.upsert_user_profile("bot", "group", "user", base_relation=10)
    assert not db.conn.in_transaction

    await db.upsert_kv_data("unrelated-write", 1)
    assert await db.get_user_profile_record("bot", "group", "user") is None
    await db.conn.execute("DROP TRIGGER reject_profile_session")
    await db.conn.commit()
    await db.upsert_user_profile("bot", "group", "user", base_relation=20)
    profile = await db.get_user_profile_record("bot", "group", "user")
    assert profile["base_relation"] == 20


@pytest.mark.asyncio
async def test_cancelled_message_write_is_rolled_back_before_other_writers(
    database, monkeypatch
):
    db = database
    inserted = asyncio.Event()

    async def wait_for_forward_messages(*args):
        inserted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        db.chat_history_repo, "_upsert_forward_messages", wait_for_forward_messages
    )
    message_task = asyncio.create_task(
        db.insert_message(
            "bot", MessageData(group_or_user_id="group", message_id="cancelled-message")
        )
    )
    unrelated_task = None
    try:
        await asyncio.wait_for(inserted.wait(), timeout=2)
        unrelated_task = asyncio.create_task(db.upsert_kv_data("unrelated-write", 1))
        await asyncio.sleep(0)
    finally:
        message_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await message_task
        if unrelated_task:
            await asyncio.wait_for(unrelated_task, timeout=2)

    assert await db.get_kv_data("unrelated-write") == 1
    assert await db.get_max_message_id("bot", "group") == 0
    assert not db.conn.in_transaction


@pytest.mark.asyncio
async def test_direct_transaction_rolls_back_all_statements_on_failure(database):
    db = database
    with pytest.raises(aiosqlite.IntegrityError):
        async with db.transaction() as conn:
            await conn.execute(
                "INSERT INTO kv_store (key, value) VALUES ('partial', 1)"
            )
            await conn.execute(
                "INSERT INTO kv_store (key, value) VALUES ('partial', 2)"
            )
    assert not db.conn.in_transaction
    await db.upsert_kv_data("complete", 1)
    assert await db.get_kv_data("partial") is None
    assert await db.get_kv_data("complete") == 1


@pytest.mark.asyncio
async def test_concurrent_messages_memory_and_relations_remain_durable(database):
    db = database

    async def update_session(index):
        await db.insert_message(
            "bot", MessageData(group_or_user_id="group", message_id=f"message-{index}")
        )
        await db.memories_repo.insert_memories(
            "bot", "group", [], batch_key=f"batch-{index}"
        )
        await db.apply_relation_change(
            "bot", "group", "user", 1, "interaction", [index + 1]
        )
        await db.upsert_kv_data(f"complete-{index}", 1)

    await asyncio.wait_for(
        asyncio.gather(*(update_session(index) for index in range(20))), timeout=10
    )
    async with db.query("SELECT COUNT(*) FROM chat_history") as cursor:
        assert (await cursor.fetchone())[0] == 20
    async with db.query(
        "SELECT COUNT(*) FROM kv_store WHERE key LIKE 'complete-%'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 20
    async with db.query(
        "SELECT COUNT(*) FROM kv_store WHERE key LIKE 'batch-%'"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 20
    assert (await db.get_relation_score("bot", "group", "user"))["total_count"] == 20
    assert not db.conn.in_transaction
