import logging
import sys
import types
import unittest

import aiosqlite


if "astrbot.api" not in sys.modules:
    astrbot_module = types.ModuleType("astrbot")
    astrbot_api_module = types.ModuleType("astrbot.api")
    astrbot_api_module.logger = logging.getLogger("astrbot")
    astrbot_module.api = astrbot_api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = astrbot_api_module

from core.database.schema import initialize_database


class RelationsMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_relations_migrate_to_user_profiles_without_profile_column(self):
        async with aiosqlite.connect(":memory:") as conn:
            await conn.execute(
                """
                CREATE TABLE user_profiles (
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
                    relation INTEGER,
                    title TEXT,
                    bot_name TEXT NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_name TEXT NOT NULL,
                    group_or_user_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    relation INTEGER,
                    title TEXT,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE kv_store (
                    key TEXT PRIMARY KEY,
                    value,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
            await conn.execute(
                """
                INSERT INTO relations (
                    bot_name, group_or_user_id, user_id, relation, title,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "giftia",
                    "group-1",
                    "user-1",
                    42,
                    "熟客",
                    "2026-08-24 10:00:00",
                    "2026-08-24 11:00:00",
                ),
            )
            await conn.commit()

            await initialize_database(conn)

            async with conn.execute("PRAGMA table_info(user_profiles)") as cursor:
                columns = await cursor.fetchall()
            self.assertNotIn("profile", {column[1] for column in columns})

            async with conn.execute(
                    """
                    SELECT relation, title, created_at, updated_at
                    FROM user_profiles
                    WHERE bot_name = ? AND group_or_user_id = ? AND user_id = ?
                    """,
                    ("giftia", "group-1", "user-1"),
            ) as cursor:
                migrated = await cursor.fetchone()
            self.assertEqual(tuple(migrated), (42, "熟客", "2026-08-24 10:00:00", "2026-08-24 11:00:00"))

            async with conn.execute(
                    "SELECT value FROM kv_store WHERE key = ?",
                    ("relations_migration_done",),
            ) as cursor:
                marker = await cursor.fetchone()
            self.assertEqual(marker[0], "1")


if __name__ == "__main__":
    unittest.main()
