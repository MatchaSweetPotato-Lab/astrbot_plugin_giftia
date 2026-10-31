"""Complete latest SQLite schema for fresh databases.

Update these definitions whenever the schema changes and append the corresponding
numbered upgrade in migrations.py. Released migration baselines remain frozen.
"""

import aiosqlite

from . import migrations

TABLE_DEFINITIONS = (
    """
    CREATE TABLE IF NOT EXISTS slang (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT NOT NULL,
        group_or_user_id TEXT NOT NULL,
        term TEXT NOT NULL COLLATE BINARY,
        description TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(bot_name, group_or_user_id, term)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT NOT NULL,
        group_or_user_id TEXT,
        nickname TEXT,
        user_id TEXT,
        message_id TEXT,
        content TEXT,
        media_ids TEXT,
        role TEXT,
        reply_decision INTEGER,
        use_rag INTEGER,
        is_recalled INTEGER,
        processing_status TEXT DEFAULT 'history',
        memory_status TEXT NOT NULL DEFAULT 'unseen',
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS forwarded_message (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        forward_id TEXT NOT NULL,
        bot_name TEXT NOT NULL,
        group_or_user_id TEXT NOT NULL,
        owner_message_id TEXT,
        source TEXT,
        source_id TEXT,
        node_count INTEGER DEFAULT 0,
        media_count INTEGER DEFAULT 0,
        nested_count INTEGER DEFAULT 0,
        content TEXT NOT NULL,
        summary TEXT,
        is_summarized INTEGER DEFAULT 0,
        query_times INTEGER DEFAULT 0,
        created_at DATETIME,
        updated_at DATETIME,
        UNIQUE(forward_id, bot_name, group_or_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS media_caption (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hash_val TEXT NOT NULL UNIQUE,
        file_name TEXT,
        url TEXT,
        media_type TEXT,
        genre TEXT,
        character TEXT,
        source TEXT,
        text TEXT,
        caption TEXT,
        is_captioned INTEGER DEFAULT 1,
        duration REAL DEFAULT 0.0,
        file_size INTEGER DEFAULT 0,
        query_times INTEGER,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_status (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT NOT NULL,
        group_or_user_id TEXT NOT NULL,
        mood TEXT,
        state TEXT,
        memory TEXT,
        action TEXT,
        energy TEXT,
        custom_status TEXT,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        account_type TEXT NOT NULL DEFAULT 'unknown'
            CHECK(account_type IN ('unknown', 'official_bot', 'qq_account')),
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
    """,
    """
    CREATE TABLE IF NOT EXISTS user_call_names (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT NOT NULL,
        group_or_user_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        call_name TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(bot_name, group_or_user_id, user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS relation_changes (
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
    """,
    """
    CREATE TABLE IF NOT EXISTS user_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT NOT NULL,
        group_or_user_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        alias TEXT NOT NULL,
        alias_count INTEGER NOT NULL DEFAULT 1,
        first_seen_at DATETIME,
        last_seen_at DATETIME,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS group_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_or_user_id TEXT NOT NULL,
        profile TEXT,
        bot_name TEXT NOT NULL,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT NOT NULL,
        group_or_user_id TEXT NOT NULL,
        memory_id TEXT NOT NULL,
        text TEXT NOT NULL,
        vector BLOB,
        metadata TEXT,
        importance INTEGER DEFAULT 5,
        hit_count INTEGER DEFAULT 0,
        last_hit_at DATETIME,
        revision INTEGER NOT NULL DEFAULT 1,
        indexed_revision INTEGER NOT NULL DEFAULT 0,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv_store (
        key TEXT PRIMARY KEY,
        value,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS short_tasks (
        task_id TEXT PRIMARY KEY,
        bot_name TEXT NOT NULL,
        group_or_user_id TEXT NOT NULL,
        creator_user_id TEXT,
        creator_nickname TEXT,
        content TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        closed_by_user_id TEXT,
        close_reason TEXT,
        expires_at DATETIME,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stickers (
        sticker_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        category TEXT,
        tags TEXT,
        description TEXT,
        filename TEXT,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stickers_bot (
        bot_name TEXT PRIMARY KEY,
        sticker_ids TEXT,
        created_at DATETIME,
        updated_at DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS token_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_name TEXT,
        group_or_user_id TEXT,
        type TEXT NOT NULL,
        provider_id TEXT,
        model_name TEXT,
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        extra_info TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        call_count INTEGER DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS token_daily_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        bot_name TEXT,
        group_or_user_id TEXT,
        type TEXT NOT NULL,
        provider_id TEXT,
        model_name TEXT,
        prompt_tokens INTEGER DEFAULT 0,
        completion_tokens INTEGER DEFAULT 0,
        total_tokens INTEGER DEFAULT 0,
        status TEXT,
        call_count INTEGER DEFAULT 0,
        UNIQUE(date, bot_name, group_or_user_id, type, provider_id, model_name, status)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_profile_sessions (
        bot_name TEXT NOT NULL,
        user_id TEXT NOT NULL,
        group_or_user_id TEXT NOT NULL,
        PRIMARY KEY (bot_name, user_id, group_or_user_id)
    )
    """,
)

INDEX_DEFINITIONS = (
    """
    CREATE INDEX IF NOT EXISTS idx_history_memory_pending ON chat_history (bot_name, group_or_user_id, id) WHERE memory_status='pending'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_group_bot ON chat_history (group_or_user_id, bot_name, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_history_bot_user_group ON chat_history (bot_name, user_id, group_or_user_id, created_at)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_message_id_unique ON chat_history (message_id, group_or_user_id, bot_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_forwarded_message_owner ON forwarded_message (bot_name, group_or_user_id, owner_message_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_forwarded_message_forward_id ON forwarded_message (bot_name, group_or_user_id, forward_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_url ON media_caption (url)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_hash_val ON media_caption (hash_val)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_group_unique ON bot_status (group_or_user_id, bot_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_relation_changes_user ON relation_changes (bot_name, user_id, id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_alias_unique ON user_aliases (bot_name, group_or_user_id, user_id, alias)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_call_names_recent
    ON user_call_names (bot_name, user_id, updated_at DESC, id DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_alias_lookup ON user_aliases (bot_name, group_or_user_id, user_id, alias_count, first_seen_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_user_alias_search ON user_aliases (bot_name, group_or_user_id, alias)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_group_id_unique ON group_profiles (group_or_user_id, bot_name)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_id_unique ON memories (memory_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_group_id_bot_name_created_at_index ON memories (group_or_user_id, bot_name, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_group_bot_importance ON memories (group_or_user_id, bot_name, importance)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_group_bot_activity ON memories (group_or_user_id, bot_name, hit_count, last_hit_at)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_kv_key_unique ON kv_store (key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_short_tasks_session_status ON short_tasks (bot_name, group_or_user_id, status, updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_short_tasks_expiry ON short_tasks (status, expires_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_token_usage_created ON token_usage (created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_token_usage_lookup ON token_usage (bot_name, group_or_user_id, type)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_token_daily_date ON token_daily_stats (date)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_user_id_unique ON user_profiles (user_id, bot_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_profile_sessions_group ON user_profile_sessions (bot_name, group_or_user_id, user_id)
    """,
)


async def initialize_database(conn: aiosqlite.Connection) -> None:
    """Create the latest schema or apply pending upgrades atomically.

    Args:
        conn: Database connection being initialized.

    Raises:
        RuntimeError: The database was created by a newer schema version.
    """
    async with conn.cursor() as cursor:
        await cursor.execute("PRAGMA journal_mode=WAL")
        await cursor.execute("SAVEPOINT initialize_database")
        try:
            await cursor.execute("PRAGMA user_version")
            version = (await cursor.fetchone())[0]
            if version > migrations.CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {version} is newer than supported "
                    f"version {migrations.CURRENT_SCHEMA_VERSION}"
                )
            await cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' LIMIT 1"
            )
            is_new = await cursor.fetchone() is None
            if is_new:
                for statement in TABLE_DEFINITIONS:
                    await cursor.execute(statement)
                for statement in INDEX_DEFINITIONS:
                    await cursor.execute(statement)
            else:
                await migrations.migrate_database(conn, version)
            # Recover interrupted batches on every startup, independent of migrations.
            await cursor.execute(
                "UPDATE chat_history SET processing_status='interrupted' "
                "WHERE processing_status IN ('pending', 'processing')"
            )
            if is_new:
                await cursor.execute(
                    f"PRAGMA user_version={migrations.CURRENT_SCHEMA_VERSION}"
                )
            await cursor.execute("RELEASE SAVEPOINT initialize_database")
        except BaseException:
            await cursor.execute("ROLLBACK TO SAVEPOINT initialize_database")
            await cursor.execute("RELEASE SAVEPOINT initialize_database")
            raise
    await conn.commit()
