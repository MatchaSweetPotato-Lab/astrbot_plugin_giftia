from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from core.database import migrations, schema


@pytest_asyncio.fixture
async def database(tmp_path):
    async with aiosqlite.connect(tmp_path / "migration.db") as conn:
        yield conn


async def schema_version(conn):
    """Read the committed or transactional SQLite schema version.

    Args:
        conn: Database connection under test.

    Returns:
        The current user_version value.
    """
    async with conn.execute("PRAGMA user_version") as cursor:
        return (await cursor.fetchone())[0]


@pytest.mark.asyncio
async def test_fresh_database_has_latest_schema_without_running_migrations(
    database, monkeypatch
):
    migration = AsyncMock(side_effect=AssertionError("Fresh database ran a migration"))
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        tuple((version, migration) for version, _ in migrations.MIGRATIONS),
    )
    statements = []
    await database.set_trace_callback(statements.append)
    for _ in range(2):
        await schema.initialize_database(database)
    migration.assert_not_awaited()
    assert await schema_version(database) == migrations.CURRENT_SCHEMA_VERSION
    assert not any("ALTER TABLE" in sql.upper() for sql in statements)
    async with database.execute("PRAGMA table_info(chat_history)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    assert {"processing_status", "memory_status"} <= columns
    async with database.execute("PRAGMA index_list(user_profiles)") as cursor:
        assert "idx_user_id_unique" in {row[1] for row in await cursor.fetchall()}
    async with database.execute("PRAGMA index_list(chat_history)") as cursor:
        assert "idx_history_bot_user_group" in {
            row[1] for row in await cursor.fetchall()
        }
    async with database.execute("PRAGMA table_info(user_profile_sessions)") as cursor:
        assert {row[1] for row in await cursor.fetchall()} == {
            "bot_name",
            "user_id",
            "group_or_user_id",
        }


@pytest.mark.asyncio
async def test_account_type_upgrade_defaults_to_unknown_and_preserves_manual_edits(
    database,
):
    await database.execute("BEGIN")
    for version, migration in migrations.MIGRATIONS:
        if version >= 4:
            break
        await migration(database)
        await database.execute(f"PRAGMA user_version={version}")
    await database.execute(
        "INSERT INTO user_profiles (bot_name, user_id, attitude, base_relation) "
        "VALUES ('bot', 'user', '已有态度', 7)"
    )
    await database.commit()

    await schema.initialize_database(database)
    async with database.execute(
        "SELECT account_type, attitude, base_relation FROM user_profiles"
    ) as cursor:
        assert await cursor.fetchone() == ("unknown", "已有态度", 7)
    await database.execute("UPDATE user_profiles SET account_type='official_bot'")
    await database.commit()
    await schema.initialize_database(database)
    async with database.execute("SELECT account_type FROM user_profiles") as cursor:
        assert await cursor.fetchone() == ("official_bot",)
    with pytest.raises(aiosqlite.IntegrityError):
        await database.execute("UPDATE user_profiles SET account_type='human'")


@pytest.mark.asyncio
async def test_unversioned_current_database_adopts_baseline_only_once(
    database, monkeypatch
):
    await schema.initialize_database(database)
    await database.execute("PRAGMA user_version=0")
    await database.commit()
    baseline = AsyncMock(wraps=migrations.MIGRATIONS[0][1])
    monkeypatch.setattr(
        migrations, "MIGRATIONS", ((1, baseline), *migrations.MIGRATIONS[1:])
    )
    for _ in range(2):
        await schema.initialize_database(database)
    baseline.assert_awaited_once()
    assert await schema_version(database) == migrations.CURRENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_legacy_upgrade_matches_the_complete_latest_schema():
    snapshots = []
    for legacy in (False, True):
        async with aiosqlite.connect(":memory:") as conn:
            if legacy:
                await conn.execute("""
                    CREATE TABLE memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        bot_name TEXT NOT NULL, group_or_user_id TEXT NOT NULL,
                        memory_id TEXT NOT NULL, text TEXT NOT NULL,
                        vector BLOB, metadata TEXT,
                        created_at DATETIME, updated_at DATETIME
                    )
                """)
                await conn.execute("""
                    CREATE TABLE user_profiles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL, group_or_user_id TEXT NOT NULL,
                        bot_name TEXT NOT NULL, created_at DATETIME, updated_at DATETIME
                    )
                """)
                await conn.commit()
            await schema.initialize_database(conn)
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ) as cursor:
                tables = [row[0] for row in await cursor.fetchall()]
            snapshot = {}
            for table in tables:
                async with conn.execute(f"PRAGMA table_info({table})") as cursor:
                    columns = {
                        row[1]: tuple(row[2:]) for row in await cursor.fetchall()
                    }
                async with conn.execute(f"PRAGMA index_list({table})") as cursor:
                    indexes = await cursor.fetchall()
                indexed_columns = {}
                for _, name, unique, origin, partial in indexes:
                    async with conn.execute(f"PRAGMA index_info({name})") as cursor:
                        indexed_columns[(name, unique, origin, partial)] = [
                            row[2] for row in await cursor.fetchall()
                        ]
                snapshot[table] = (columns, indexed_columns)
            snapshots.append(snapshot)
    assert snapshots[0] == snapshots[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_pending_migrations_run_in_order_and_do_not_repeat(
    database, monkeypatch, legacy
):
    if legacy:
        await database.execute("""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_name TEXT NOT NULL, group_or_user_id TEXT NOT NULL,
                memory_id TEXT NOT NULL, text TEXT NOT NULL,
                vector BLOB, metadata TEXT, created_at DATETIME, updated_at DATETIME
            )
        """)
    else:
        await schema.initialize_database(database)
    version = migrations.CURRENT_SCHEMA_VERSION
    calls = []

    async def add_note(conn):
        calls.append(await schema_version(conn))
        await conn.execute("CREATE TABLE migration_notes (note TEXT NOT NULL)")
        await conn.execute("ALTER TABLE memories ADD COLUMN upgrade_note TEXT")

    async def fill_note(conn):
        calls.append(await schema_version(conn))
        await conn.execute("UPDATE memories SET upgrade_note='upgraded'")

    await database.execute(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text) "
        "VALUES ('bot', 'group', 'saved', 'Original fact')"
    )
    await database.commit()
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (*migrations.MIGRATIONS, (version + 1, add_note), (version + 2, fill_note)),
    )
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", version + 2)
    monkeypatch.setattr(
        schema,
        "TABLE_DEFINITIONS",
        (
            *schema.TABLE_DEFINITIONS,
            "CREATE TABLE IF NOT EXISTS migration_notes (note TEXT NOT NULL)",
        ),
    )
    for _ in range(2):
        await schema.initialize_database(database)
    assert calls == [version, version + 1]
    assert await schema_version(database) == version + 2
    async with database.execute("SELECT text, upgrade_note FROM memories") as cursor:
        assert await cursor.fetchone() == ("Original fact", "upgraded")


@pytest.mark.asyncio
async def test_failed_upgrade_rolls_back_schema_data_and_version(
    database, monkeypatch, tmp_path
):
    await schema.initialize_database(database)
    version = migrations.CURRENT_SCHEMA_VERSION
    await database.execute(
        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text) "
        "VALUES ('bot', 'group', 'saved', 'Original fact')"
    )
    await database.commit()

    async def change_fact(conn):
        await conn.execute("ALTER TABLE memories ADD COLUMN upgrade_note TEXT")
        await conn.execute("UPDATE memories SET text='Changed fact'")

    fail = AsyncMock(side_effect=aiosqlite.OperationalError("Forced migration failure"))
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (*migrations.MIGRATIONS, (version + 1, change_fact), (version + 2, fail)),
    )
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", version + 2)
    with pytest.raises(aiosqlite.OperationalError, match="Forced migration failure"):
        await schema.initialize_database(database)
    assert not database.in_transaction
    async with aiosqlite.connect(tmp_path / "migration.db") as reopened:
        assert await schema_version(reopened) == version
        async with reopened.execute("PRAGMA table_info(memories)") as cursor:
            assert "upgrade_note" not in {row[1] for row in await cursor.fetchall()}
        async with reopened.execute("SELECT text FROM memories") as cursor:
            assert await cursor.fetchone() == ("Original fact",)

    fail.side_effect = None
    await schema.initialize_database(database)
    assert await schema_version(database) == version + 2
    assert fail.await_count == 2


@pytest.mark.asyncio
async def test_failed_fresh_initialization_does_not_leave_partial_schema(
    database, monkeypatch
):
    monkeypatch.setattr(
        schema,
        "INDEX_DEFINITIONS",
        (*schema.INDEX_DEFINITIONS, "CREATE INDEX bad_index ON missing_table (id)"),
    )
    with pytest.raises(aiosqlite.OperationalError, match="missing_table"):
        await schema.initialize_database(database)
    assert await schema_version(database) == 0
    assert not database.in_transaction
    async with database.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ) as cursor:
        assert await cursor.fetchall() == []


@pytest.mark.asyncio
async def test_newer_database_is_rejected_before_any_schema_changes(database):
    version = migrations.CURRENT_SCHEMA_VERSION + 1
    await database.execute(f"PRAGMA user_version={version}")
    with pytest.raises(RuntimeError, match="newer than supported"):
        await schema.initialize_database(database)
    assert await schema_version(database) == version
    assert not database.in_transaction
    async with database.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cursor:
        assert await cursor.fetchall() == []


@pytest.mark.asyncio
async def test_missing_migration_step_is_rejected(database, monkeypatch):
    await schema.initialize_database(database)
    version = migrations.CURRENT_SCHEMA_VERSION
    skipped = AsyncMock()
    monkeypatch.setattr(
        migrations, "MIGRATIONS", (*migrations.MIGRATIONS, (version + 2, skipped))
    )
    monkeypatch.setattr(migrations, "CURRENT_SCHEMA_VERSION", version + 2)
    with pytest.raises(RuntimeError, match="Missing database migration"):
        await schema.initialize_database(database)
    skipped.assert_not_awaited()
    assert await schema_version(database) == version
