from datetime import datetime

import aiosqlite

from astrbot.api import logger

from .utils import parse_aliases


async def initialize_database(conn: aiosqlite.Connection) -> None:
    async with conn.cursor() as cursor:
        # 开启 WAL 模式，提高并发性能
        await cursor.execute("PRAGMA journal_mode=WAL;")
        # 创建聊天记录表
        await cursor.execute("""
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
                created_at DATETIME,
                updated_at DATETIME
            )
        """)  # 0: 判断拒绝, 1: 判断通过, 2: 未审查
        # 自动迁移已有的 chat_history 表，添加 role 列
        try:
            await cursor.execute("ALTER TABLE chat_history ADD COLUMN role TEXT")
        except aiosqlite.OperationalError as e:
            if (
                "duplicate" not in str(e).lower()
                and "already exists" not in str(e).lower()
            ):
                logger.warning(f"Failed to add role column to chat_history: {e}")
        # 创建索引
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_bot ON chat_history (group_or_user_id, bot_name, created_at)"
        )
        await cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_message_id_unique ON chat_history (message_id, group_or_user_id, bot_name)"
        )
        # 创建合并转发消息表。聊天记录只保留 [合并转发:id]，完整内容和后续转述缓存放这里。
        await cursor.execute("""
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
        """)
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_forwarded_message_owner ON forwarded_message (bot_name, group_or_user_id, owner_message_id)"
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_forwarded_message_forward_id ON forwarded_message (bot_name, group_or_user_id, forward_id)"
        )
        # 创建媒体的文字转述（caption）表
        await cursor.execute("""
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
        """)
        for col_def in ("is_captioned INTEGER DEFAULT 1", "duration REAL DEFAULT 0.0", "file_size INTEGER DEFAULT 0"):
            try:
                await cursor.execute(f"ALTER TABLE media_caption ADD COLUMN {col_def}")
            except aiosqlite.OperationalError as e:
                if (
                    "duplicate" not in str(e).lower()
                    and "already exists" not in str(e).lower()
                ):
                    logger.warning(
                        f"Failed to add column {col_def} to media_caption: {e}"
                    )
        # 创建索引
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_url ON media_caption (url)"
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_hash_val ON media_caption (hash_val)"
        )
        # 创建机器人状态表
        await cursor.execute("""
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
        """)
        try:
            await cursor.execute("ALTER TABLE bot_status ADD COLUMN custom_status TEXT")
        except aiosqlite.OperationalError as e:
            if "duplicate" not in str(e).lower() and "already exists" not in str(e).lower():
                logger.warning(f"Failed to migrate bot_status column: {e}")
        # 创建索引
        await cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_group_unique ON bot_status (group_or_user_id, bot_name)"
        )
        # 创建用户画像表
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_or_user_id TEXT NOT NULL,
                call_name TEXT,
                aliases TEXT,
                personality TEXT,
                interests TEXT,
                attitude TEXT,
                agreements TEXT,
                extra TEXT,
                avatar_description TEXT,
                relation INTEGER,
                title TEXT,
                bot_name TEXT NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
        """)
        for column_sql in (
            "ALTER TABLE user_profiles ADD COLUMN call_name TEXT",
            "ALTER TABLE user_profiles ADD COLUMN aliases TEXT",
            "ALTER TABLE user_profiles ADD COLUMN personality TEXT",
            "ALTER TABLE user_profiles ADD COLUMN interests TEXT",
            "ALTER TABLE user_profiles ADD COLUMN attitude TEXT",
            "ALTER TABLE user_profiles ADD COLUMN agreements TEXT",
            "ALTER TABLE user_profiles ADD COLUMN extra TEXT",
            "ALTER TABLE user_profiles ADD COLUMN avatar_description TEXT",
            "ALTER TABLE user_profiles ADD COLUMN relation INTEGER",
            "ALTER TABLE user_profiles ADD COLUMN title TEXT",
        ):
            try:
                await cursor.execute(column_sql)
            except aiosqlite.OperationalError as e:
                if (
                    "duplicate" not in str(e).lower()
                    and "already exists" not in str(e).lower()
                ):
                    logger.warning(
                        f"Failed to migrate user_profiles column: {e}"
                    )
        # 创建索引
        await cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_id_unique ON user_profiles (user_id, group_or_user_id, bot_name)"
        )
        # 用户外号统计表。aliases 旧列保留兼容，但读写权威来源迁移到这里。
        await cursor.execute("""
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
        """)
        await cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_alias_unique ON user_aliases (bot_name, group_or_user_id, user_id, alias)"
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_alias_lookup ON user_aliases (bot_name, group_or_user_id, user_id, alias_count, first_seen_at)"
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_alias_search ON user_aliases (bot_name, group_or_user_id, alias)"
        )
        # 创建群画像表
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_or_user_id TEXT NOT NULL,
                profile TEXT,
                bot_name TEXT NOT NULL,
                created_at DATETIME,
                updated_at DATETIME
            )
        """)
        # 创建索引
        await cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_group_id_unique ON group_profiles (group_or_user_id, bot_name)"
        )
        # 创建记忆缓存表
        await cursor.execute("""
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
                created_at DATETIME,
                updated_at DATETIME
            )
        """)
        for sql, column_name in (
            (
                "ALTER TABLE memories ADD COLUMN importance INTEGER DEFAULT 5",
                "importance",
            ),
            (
                "ALTER TABLE memories ADD COLUMN hit_count INTEGER DEFAULT 0",
                "hit_count",
            ),
            (
                "ALTER TABLE memories ADD COLUMN last_hit_at DATETIME",
                "last_hit_at",
            ),
        ):
            try:
                await cursor.execute(sql)
            except aiosqlite.OperationalError as e:
                if (
                    "duplicate" not in str(e).lower()
                    and "already exists" not in str(e).lower()
                ):
                    logger.warning(
                        f"Failed to add {column_name} column to memories: {e}"
                    )
        # 创建索引
        await cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_id_unique ON memories (memory_id)"
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_group_id_bot_name_created_at_index ON memories (group_or_user_id, bot_name, created_at)"
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_group_bot_importance ON memories (group_or_user_id, bot_name, importance)"
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_group_bot_activity ON memories (group_or_user_id, bot_name, hit_count, last_hit_at)"
        )
        # 创建键值对表
        await cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        # 创建索引
        await cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_kv_key_unique ON kv_store (key)"
        )
        await cursor.execute(
            "SELECT value FROM kv_store WHERE key = ? LIMIT 1",
            ("user_aliases_migration_done",),
        )
        row = await cursor.fetchone()
        if not row:
            logger.info("[Database] Running user aliases migration...")
            async with conn.execute(
                """
                SELECT bot_name, group_or_user_id, user_id, aliases, created_at, updated_at
                FROM user_profiles
                WHERE aliases IS NOT NULL AND aliases != ''
                """
            ) as alias_cursor:
                alias_rows = await alias_cursor.fetchall()
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
        # 创建短期任务看板表
        await cursor.execute("""
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
        """)
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_short_tasks_session_status ON short_tasks (bot_name, group_or_user_id, status, updated_at)"
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_short_tasks_expiry ON short_tasks (status, expires_at)"
        )
        # 创建关系表
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_name TEXT NOT NULL,
                group_or_user_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                relation INTEGER,
                title TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
        """)
        # 创建索引
        await cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_relation_unique ON relations (user_id, group_or_user_id, bot_name)"
        )
        # 检查关系数据回填的迁移是否已运行，避免每次启动都对全表进行重型更新与插入扫描
        await cursor.execute(
            "SELECT value FROM kv_store WHERE key = ? LIMIT 1",
            ("relations_migration_done",)
        )
        row = await cursor.fetchone()
        if not row:
            logger.info("[Database] Running relations schema migration to user_profiles...")
            await cursor.execute("""
                UPDATE user_profiles
                SET
                    relation = COALESCE(
                        relation,
                        (
                            SELECT r.relation
                            FROM relations r
                            WHERE r.user_id = user_profiles.user_id
                              AND r.group_or_user_id = user_profiles.group_or_user_id
                              AND r.bot_name = user_profiles.bot_name
                            LIMIT 1
                        )
                    ),
                    title = COALESCE(
                        title,
                        (
                            SELECT r.title
                            FROM relations r
                            WHERE r.user_id = user_profiles.user_id
                              AND r.group_or_user_id = user_profiles.group_or_user_id
                              AND r.bot_name = user_profiles.bot_name
                            LIMIT 1
                        )
                    )
                WHERE EXISTS (
                    SELECT 1
                    FROM relations r
                    WHERE r.user_id = user_profiles.user_id
                      AND r.group_or_user_id = user_profiles.group_or_user_id
                      AND r.bot_name = user_profiles.bot_name
                )
            """)
            await cursor.execute("""
                INSERT INTO user_profiles (
                    user_id,
                    group_or_user_id,
                    bot_name,
                    relation,
                    title,
                    created_at,
                    updated_at
                )
                SELECT
                    r.user_id,
                    r.group_or_user_id,
                    r.bot_name,
                    r.relation,
                    r.title,
                    COALESCE(r.created_at, CURRENT_TIMESTAMP),
                    COALESCE(r.updated_at, CURRENT_TIMESTAMP)
                FROM relations r
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM user_profiles up
                    WHERE up.user_id = r.user_id
                      AND up.group_or_user_id = r.group_or_user_id
                      AND up.bot_name = r.bot_name
                )
            """)
            update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await cursor.execute(
                """
                INSERT INTO kv_store (key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                ("relations_migration_done", "1", update_time, update_time)
            )
            logger.info("[Database] Relations schema migration completed.")

        # 创建表情包表
        await cursor.execute("""
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
        """)
        # 机器人表情包列表
        await cursor.execute("""
            CREATE TABLE IF NOT EXISTS stickers_bot (
                bot_name TEXT PRIMARY KEY,
                sticker_ids TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
        """)
        # Token 消耗统计表
        await cursor.execute("""
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
        """)
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_created ON token_usage (created_at)"
        )
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_usage_lookup ON token_usage (bot_name, group_or_user_id, type)"
        )
        # Token 每日拍扁统计历史表
        await cursor.execute("""
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
        """)
        await cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_daily_date ON token_daily_stats (date)"
        )
        
        # 数据库迁移：为已存在的表添加 call_count 字段
        try:
            await cursor.execute("ALTER TABLE token_usage ADD COLUMN call_count INTEGER DEFAULT 1")
        except Exception:
            pass
        try:
            await cursor.execute("ALTER TABLE token_daily_stats ADD COLUMN call_count INTEGER DEFAULT 0")
        except Exception:
            pass
    # 提交
    await conn.commit()
