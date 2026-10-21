"""Ordered SQLite upgrades, starting with legacy schemas through Giftia v0.4.5.

Append future numbered migrations instead of changing released steps, and keep
schema.py's fresh-database snapshot current. Migrations must not commit: schema
changes, data backfills and the version marker share initialization's transaction.
"""

from datetime import datetime

import aiosqlite

from astrbot.api import logger

from .schema_v1 import INDEX_DEFINITIONS, TABLE_DEFINITIONS
from .utils import parse_aliases


async def _add_missing_columns(
    conn: aiosqlite.Connection,
    table: str,
    definitions: tuple[tuple[str, str], ...],
) -> set[str]:
    """Add absent legacy columns without suppressing migration failures.

    Args:
        conn: Database connection being initialized.
        table: Internal table name supplied by a migration.
        definitions: Column names and SQL definitions in dependency order.

    Returns:
        Names of columns added during this upgrade.
    """
    async with conn.execute(f"PRAGMA table_info({table})") as cursor:
        existing = {row[1] for row in await cursor.fetchall()}
    added = set()
    for column, definition in definitions:
        if column not in existing:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            added.add(column)
    return added


async def migrate_chat_history(conn: aiosqlite.Connection) -> bool:
    """Add chat fields and report whether legacy observation needs backfilling.

    Args:
        conn: Database connection being initialized.

    Returns:
        Whether this upgrade added the memory_status column.
    """
    added = await _add_missing_columns(
        conn,
        "chat_history",
        (
            ("role", "TEXT"),
            ("processing_status", "TEXT DEFAULT 'history'"),
            ("memory_status", "TEXT NOT NULL DEFAULT 'unseen'"),
        ),
    )
    return "memory_status" in added


async def migrate_media_captions(conn: aiosqlite.Connection) -> None:
    """Add fields introduced after the original media caption schema.

    Args:
        conn: Database connection being initialized.
    """
    await _add_missing_columns(
        conn,
        "media_caption",
        (
            ("is_captioned", "INTEGER DEFAULT 1"),
            ("duration", "REAL DEFAULT 0.0"),
            ("file_size", "INTEGER DEFAULT 0"),
        ),
    )


async def migrate_bot_status(conn: aiosqlite.Connection) -> None:
    """Add the custom status field to legacy bot status tables.

    Args:
        conn: Database connection being initialized.
    """
    await _add_missing_columns(conn, "bot_status", (("custom_status", "TEXT"),))


async def migrate_user_profile_columns(conn: aiosqlite.Connection) -> None:
    """Complete legacy profiles before migrating aliases and merging sessions.

    Args:
        conn: Database connection being initialized.
    """
    await _add_missing_columns(
        conn,
        "user_profiles",
        (
            ("call_name", "TEXT"),
            ("aliases", "TEXT"),
            ("personality", "TEXT"),
            ("interests", "TEXT"),
            ("attitude", "TEXT"),
            ("agreements", "TEXT"),
            ("extra", "TEXT"),
            ("avatar_description", "TEXT"),
            ("base_relation", "INTEGER NOT NULL DEFAULT 0"),
            ("title", "TEXT"),
            ("attitude_evaluated_change_id", "INTEGER NOT NULL DEFAULT 0"),
            ("relation_history_revision", "INTEGER NOT NULL DEFAULT 0"),
        ),
    )


async def migrate_memories(conn: aiosqlite.Connection) -> None:
    """Complete legacy facts and queue unversioned vectors for reindexing.

    Args:
        conn: Database connection being initialized.
    """
    await _add_missing_columns(
        conn,
        "memories",
        (
            ("importance", "INTEGER DEFAULT 5"),
            ("hit_count", "INTEGER DEFAULT 0"),
            ("last_hit_at", "DATETIME"),
            ("revision", "INTEGER NOT NULL DEFAULT 1"),
            ("indexed_revision", "INTEGER NOT NULL DEFAULT 0"),
        ),
    )


async def migrate_memory_progress(conn: aiosqlite.Connection) -> None:
    """Backfill newly added memory observation state from legacy progress once.

    Args:
        conn: Database connection being initialized.
    """
    async with conn.cursor() as cursor:
        # Preserve legacy progress without treating unprocessed history as seen.
        await cursor.execute("""
            UPDATE chat_history SET memory_status='summarized'
            WHERE id <= COALESCE((
                SELECT CASE
                    WHEN typeof(value)='integer' THEN value
                    WHEN typeof(value)='text' AND value != ''
                      AND value NOT GLOB '*[^0-9]*' THEN CAST(value AS INTEGER)
                    ELSE 0
                END
                FROM kv_store
                WHERE key='passive_memory:last_summarized_id:'
                    || chat_history.bot_name || ':' || chat_history.group_or_user_id
            ), 0)
        """)
        await cursor.execute(
            "DELETE FROM kv_store WHERE key LIKE 'passive_memory:silence_armed:%' "
            "OR key LIKE 'passive_memory:silent_count:%'"
        )


async def migrate_user_aliases(conn: aiosqlite.Connection) -> None:
    """Copy legacy profile aliases into counted aliases before profiles are merged.

    Args:
        conn: Database connection being initialized.
    """
    async with conn.cursor() as cursor:
        await cursor.execute(
            "SELECT value FROM kv_store WHERE key = ? LIMIT 1",
            ("user_aliases_migration_done",),
        )
        row = await cursor.fetchone()
        if not row:
            logger.info("[Database] Running user aliases migration...")
            async with conn.execute(
                "PRAGMA table_info(user_profiles)"
            ) as columns_cursor:
                legacy_profile = any(
                    column[1] == "group_or_user_id"
                    for column in await columns_cursor.fetchall()
                )
            alias_rows = []
            if legacy_profile:
                async with conn.execute(
                    """
                    SELECT bot_name, group_or_user_id, user_id, aliases, created_at, updated_at
                    FROM user_profiles
                    WHERE aliases IS NOT NULL AND aliases != ''
                    """
                ) as alias_cursor:
                    names = [column[0] for column in alias_cursor.description]
                    alias_rows = [
                        dict(zip(names, row)) for row in await alias_cursor.fetchall()
                    ]
            for alias_row in alias_rows:
                aliases = parse_aliases(alias_row["aliases"])
                first_seen_at = alias_row["created_at"] or datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                last_seen_at = alias_row["updated_at"] or first_seen_at
                for alias in aliases:
                    await cursor.execute(
                        """
                        INSERT INTO user_aliases (
                            bot_name,
                            group_or_user_id,
                            user_id,
                            alias,
                            alias_count,
                            first_seen_at,
                            last_seen_at,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                        ON CONFLICT(bot_name, group_or_user_id, user_id, alias) DO NOTHING
                        """,
                        (
                            alias_row["bot_name"],
                            alias_row["group_or_user_id"],
                            alias_row["user_id"],
                            alias,
                            first_seen_at,
                            last_seen_at,
                            first_seen_at,
                            last_seen_at,
                        ),
                    )
            update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await cursor.execute(
                """
                INSERT INTO kv_store (key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                ("user_aliases_migration_done", "1", update_time, update_time),
            )
            logger.info("[Database] User aliases migration completed.")


async def migrate_shared_user_profiles(conn: aiosqlite.Connection) -> None:
    """Merge legacy session profiles and remove unused affinity snapshots atomically.

    Args:
        conn: Database connection being initialized.
    """
    await conn.execute("SAVEPOINT shared_user_profiles")
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile_sessions (
                bot_name TEXT NOT NULL,
                user_id TEXT NOT NULL,
                group_or_user_id TEXT NOT NULL,
                PRIMARY KEY (bot_name, user_id, group_or_user_id)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_profile_sessions_group "
            "ON user_profile_sessions (bot_name, group_or_user_id, user_id)"
        )
        async with conn.execute("PRAGMA table_info(user_profiles)") as cursor:
            profile_columns = {row[1] for row in await cursor.fetchall()}
        if "group_or_user_id" in profile_columns:
            await conn.execute(
                "INSERT OR IGNORE INTO user_profile_sessions "
                "SELECT bot_name, user_id, group_or_user_id FROM user_profiles"
            )
            async with conn.execute(
                "SELECT * FROM user_profiles ORDER BY "
                "COALESCE(julianday(updated_at), julianday(created_at), 0) DESC, id DESC"
            ) as cursor:
                names = [column[0] for column in cursor.description]
                rows = [dict(zip(names, row)) for row in await cursor.fetchall()]
            await conn.execute("""
                CREATE TABLE user_profiles_shared (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    call_name TEXT,
                    aliases TEXT,
                    personality TEXT,
                    interests TEXT,
                    attitude TEXT,
                    agreements TEXT,
                    extra TEXT,
                    avatar_description TEXT,
                    base_relation INTEGER NOT NULL DEFAULT 0,
                    title TEXT,
                    attitude_evaluated_change_id INTEGER NOT NULL DEFAULT 0,
                    relation_history_revision INTEGER NOT NULL DEFAULT 0,
                    bot_name TEXT NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME
                )
            """)
            fields = (
                "call_name",
                "personality",
                "interests",
                "agreements",
                "extra",
                "avatar_description",
                "title",
            )
            merged = {}
            for row in rows:
                key = (row["bot_name"], row["user_id"])
                if key not in merged:
                    merged[key] = {
                        **row,
                        "attitude": None,
                        "aliases": None,
                        "attitude_evaluated_change_id": 0,
                    }
                target = merged[key]
                target["base_relation"] = max(
                    target["base_relation"], row["base_relation"]
                )
                target["relation_history_revision"] = max(
                    target["relation_history_revision"],
                    row["relation_history_revision"],
                )
                for field in fields:
                    if (
                        not str(target.get(field) or "").strip()
                        and str(row.get(field) or "").strip()
                    ):
                        target[field] = row[field]
            columns = (
                "id",
                "user_id",
                "call_name",
                "aliases",
                "personality",
                "interests",
                "attitude",
                "agreements",
                "extra",
                "avatar_description",
                "base_relation",
                "title",
                "attitude_evaluated_change_id",
                "relation_history_revision",
                "bot_name",
                "created_at",
                "updated_at",
            )
            await conn.executemany(
                f"INSERT INTO user_profiles_shared ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [tuple(row[column] for column in columns) for row in merged.values()],
            )
            await conn.execute("DROP TABLE user_profiles")
            await conn.execute(
                "ALTER TABLE user_profiles_shared RENAME TO user_profiles"
            )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_id_unique "
            "ON user_profiles (user_id, bot_name)"
        )
        async with conn.execute("PRAGMA table_info(relation_changes)") as cursor:
            history_columns = {row[1] for row in await cursor.fetchall()}
        if {"relation_before", "relation_after"} & history_columns:
            async with conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='relation_changes'"
            ) as cursor:
                sequence = await cursor.fetchone()
            await conn.execute("""
                CREATE TABLE relation_changes_shared (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_name TEXT NOT NULL,
                    group_or_user_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    delta INTEGER NOT NULL CHECK(delta BETWEEN -5 AND 5 AND delta != 0),
                    reason TEXT NOT NULL,
                    source_ids TEXT,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(bot_name, group_or_user_id, user_id, source_ids)
                )
            """)
            await conn.execute(
                "INSERT INTO relation_changes_shared SELECT id, bot_name, group_or_user_id, "
                "user_id, delta, reason, source_ids, occurred_at, created_at FROM relation_changes"
            )
            await conn.execute("DROP TABLE relation_changes")
            await conn.execute(
                "ALTER TABLE relation_changes_shared RENAME TO relation_changes"
            )
            if sequence:
                await conn.execute(
                    "UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='relation_changes'",
                    (sequence[0],),
                )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_relation_changes_user ON relation_changes (bot_name, user_id, id)"
        )
        await conn.execute("RELEASE SAVEPOINT shared_user_profiles")
    except BaseException:
        await conn.execute("ROLLBACK TO SAVEPOINT shared_user_profiles")
        await conn.execute("RELEASE SAVEPOINT shared_user_profiles")
        raise


async def migrate_token_usage(conn: aiosqlite.Connection) -> None:
    """Add call counters to legacy raw and daily token statistics.

    Args:
        conn: Database connection being initialized.
    """
    for table, default in (("token_usage", 1), ("token_daily_stats", 0)):
        await _add_missing_columns(
            conn, table, (("call_count", f"INTEGER DEFAULT {default}"),)
        )


async def _migrate_legacy_to_v1(conn: aiosqlite.Connection) -> None:
    """Adopt all unversioned schemas through Giftia v0.4.5 as schema version 1.

    Args:
        conn: Database connection being initialized.
    """
    for statement in TABLE_DEFINITIONS:
        await conn.execute(statement)
    memory_status_added = await migrate_chat_history(conn)
    await migrate_media_captions(conn)
    await migrate_bot_status(conn)
    await migrate_user_profile_columns(conn)
    await migrate_memories(conn)
    await migrate_token_usage(conn)
    if memory_status_added:
        await migrate_memory_progress(conn)
    # Alias upserts need their conflict target before profiles lose session IDs.
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_alias_unique "
        "ON user_aliases (bot_name, group_or_user_id, user_id, alias)"
    )
    await migrate_user_aliases(conn)
    await migrate_shared_user_profiles(conn)
    for statement in INDEX_DEFINITIONS:
        await conn.execute(statement)


# Schema versions are independent of plugin releases. Append (2, migrate_to_v2),
# then (3, migrate_to_v3), etc.; never renumber or remove an applied migration.
MIGRATIONS = ((1, _migrate_legacy_to_v1),)
CURRENT_SCHEMA_VERSION = MIGRATIONS[-1][0]


async def migrate_database(conn: aiosqlite.Connection, version: int) -> None:
    """Apply only pending numbered steps within the caller's transaction.

    Args:
        conn: Database connection being initialized.
        version: Previously committed PRAGMA user_version value.

    Raises:
        RuntimeError: The migration sequence has a missing version.
    """
    for target, migration in MIGRATIONS:
        if target <= version:
            continue
        if target != version + 1:
            raise RuntimeError(
                f"Missing database migration after schema version {version}"
            )
        logger.info("[Database] Migrating schema %s -> %s", version, target)
        await migration(conn)
        await conn.execute(f"PRAGMA user_version={target}")
        version = target
