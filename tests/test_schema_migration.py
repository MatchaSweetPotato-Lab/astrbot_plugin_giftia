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


class AffinitySchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_schema_has_shared_profiles_without_affinity_snapshots(self):
        async with aiosqlite.connect(":memory:") as conn:
            for _ in range(2):
                await initialize_database(conn)
            async with conn.execute("PRAGMA table_info(user_profiles)") as cursor:
                columns = {row[1]: row for row in await cursor.fetchall()}
            self.assertNotIn("relation", columns)
            self.assertNotIn("group_or_user_id", columns)
            self.assertEqual(columns["base_relation"][4], "0")
            async with conn.execute("PRAGMA table_info(relation_changes)") as cursor:
                columns = {row[1]: row[2] for row in await cursor.fetchall()}
            self.assertNotIn("relation_before", columns)
            self.assertNotIn("relation_after", columns)
            async with conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='relations'"
            ) as cursor:
                self.assertEqual((await cursor.fetchone())[0], 0)


class MemorySchemaTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_memories_gain_missing_columns_without_losing_data(self):
        for has_statistics in (False, True):
            with self.subTest(has_statistics=has_statistics):
                async with aiosqlite.connect(":memory:") as conn:
                    conn.row_factory = aiosqlite.Row
                    await conn.execute("""
                        CREATE TABLE memories (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            bot_name TEXT NOT NULL,
                            group_or_user_id TEXT NOT NULL,
                            memory_id TEXT NOT NULL,
                            text TEXT NOT NULL,
                            vector BLOB,
                            metadata TEXT,
                            created_at DATETIME,
                            updated_at DATETIME
                        )
                    """)
                    if has_statistics:
                        await conn.execute(
                            "ALTER TABLE memories ADD COLUMN importance INTEGER DEFAULT 5"
                        )
                        await conn.execute(
                            "ALTER TABLE memories ADD COLUMN hit_count INTEGER DEFAULT 0"
                        )
                        await conn.execute(
                            "ALTER TABLE memories ADD COLUMN last_hit_at DATETIME"
                        )
                    await conn.executemany(
                        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, "
                        "vector, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                "bot",
                                "group",
                                memory_id,
                                "Existing factual memory",
                                vector,
                                '{"user_id":"user","associated_user_ids":["user"]}',
                                "2026-10-01 12:00:00",
                                "2026-10-02 12:00:00",
                            )
                            for memory_id, vector in (
                                ("with-vector", b"\x01\x02"),
                                ("without-vector", None),
                            )
                        ],
                    )
                    if has_statistics:
                        await conn.execute(
                            "UPDATE memories SET importance=9, hit_count=4, "
                            "last_hit_at='2026-10-03 12:00:00'"
                        )
                    await conn.commit()
                    async with conn.execute(
                        "SELECT * FROM memories ORDER BY id"
                    ) as cursor:
                        original = [dict(row) for row in await cursor.fetchall()]

                    await initialize_database(conn)
                    async with conn.execute("PRAGMA table_info(memories)") as cursor:
                        columns = {row["name"]: row for row in await cursor.fetchall()}
                    self.assertEqual(columns["revision"]["notnull"], 1)
                    self.assertEqual(columns["revision"]["dflt_value"], "1")
                    self.assertEqual(columns["indexed_revision"]["notnull"], 1)
                    self.assertEqual(columns["indexed_revision"]["dflt_value"], "0")
                    async with conn.execute(
                        "SELECT * FROM memories ORDER BY id"
                    ) as cursor:
                        migrated = [dict(row) for row in await cursor.fetchall()]
                    for before, after in zip(original, migrated, strict=True):
                        self.assertEqual({key: after[key] for key in before}, before)
                        self.assertEqual(after["revision"], 1)
                        self.assertEqual(after["indexed_revision"], 0)
                        self.assertEqual(
                            after["importance"], 9 if has_statistics else 5
                        )
                        self.assertEqual(after["hit_count"], 4 if has_statistics else 0)
                        if not has_statistics:
                            self.assertIsNone(after["last_hit_at"])
                    async with conn.execute(
                        "SELECT memory_id FROM memories WHERE indexed_revision<revision ORDER BY id"
                    ) as cursor:
                        self.assertEqual(
                            [row[0] for row in await cursor.fetchall()],
                            ["with-vector", "without-vector"],
                        )

                    await initialize_database(conn)
                    async with conn.execute(
                        "SELECT * FROM memories ORDER BY id"
                    ) as cursor:
                        self.assertEqual(
                            [dict(row) for row in await cursor.fetchall()], migrated
                        )

    async def test_partial_revision_migration_preserves_existing_column(self):
        for missing in ("revision", "indexed_revision"):
            with self.subTest(missing=missing):
                async with aiosqlite.connect(":memory:") as conn:
                    await initialize_database(conn)
                    await conn.execute(
                        "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, "
                        "revision, indexed_revision) VALUES ('bot', 'group', 'saved', 'Fact', 7, 0)"
                    )
                    await conn.execute(f"ALTER TABLE memories DROP COLUMN {missing}")
                    await conn.execute("PRAGMA user_version=0")
                    await conn.commit()
                    for _ in range(2):
                        await initialize_database(conn)
                        async with conn.execute(
                            "SELECT revision, indexed_revision FROM memories"
                        ) as cursor:
                            self.assertEqual(
                                tuple(await cursor.fetchone()),
                                (1 if missing == "revision" else 7, 0),
                            )

    async def test_restart_preserves_current_index_revisions(self):
        async with aiosqlite.connect(":memory:") as conn:
            await initialize_database(conn)
            await conn.executemany(
                "INSERT INTO memories (bot_name, group_or_user_id, memory_id, text, vector, "
                "revision, indexed_revision) VALUES ('bot', 'group', ?, 'Fact', X'01', ?, ?)",
                [("indexed", 7, 7), ("stale", 8, 7), ("pending", 4, 0)],
            )
            await conn.commit()
            for _ in range(2):
                await initialize_database(conn)
                async with conn.execute(
                    "SELECT memory_id, revision, indexed_revision FROM memories ORDER BY id"
                ) as cursor:
                    self.assertEqual(
                        await cursor.fetchall(),
                        [("indexed", 7, 7), ("stale", 8, 7), ("pending", 4, 0)],
                    )


if __name__ == "__main__":
    unittest.main()
