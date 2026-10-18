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
    async def test_fresh_schema_has_base_and_real_scores_without_legacy_relations(self):
        async with aiosqlite.connect(":memory:") as conn:
            for _ in range(2):
                await initialize_database(conn)
            async with conn.execute("PRAGMA table_info(user_profiles)") as cursor:
                columns = {row[1]: row for row in await cursor.fetchall()}
            self.assertNotIn("relation", columns)
            self.assertEqual(columns["base_relation"][4], "0")
            async with conn.execute("PRAGMA table_info(relation_changes)") as cursor:
                columns = {row[1]: row[2] for row in await cursor.fetchall()}
            self.assertEqual(columns["relation_before"], "REAL")
            self.assertEqual(columns["relation_after"], "REAL")
            async with conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='relations'"
            ) as cursor:
                self.assertEqual((await cursor.fetchone())[0], 0)


if __name__ == "__main__":
    unittest.main()
