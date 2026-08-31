"""meme_manager 表情包集成测试。

覆盖：
1. MemeManagerClient 数据库读取、人设隔离过滤（通用 * 与专属人设）、标签评分排序、Top K 截取。
2. get_decision_rules 条件生成（use_meme_manager=True/False）。
3. build_xml_instructions 条件提示词（use_meme_manager=True 时剥离 <add_sticker>）。
4. decode_decision_xml 对 meme_tags 的解析。
5. XmlParse._load_sticker_image 对 mm_ 短 ID 的解析与组件构建。
"""

import asyncio
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _stub_module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class FakeImage:
    """Image 组件替身"""

    def __init__(self, file: str = "", url: str = ""):
        self.file = file
        self.url = url
        self.sub_type = None
        self.meme_desc = None

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=str(path))

    @classmethod
    def fromURL(cls, url):
        return cls(url=url)


if "astrbot.api" not in sys.modules:
    _stub_module("astrbot")
    _stub_module("astrbot.api", logger=logging.getLogger("astrbot"))
    _stub_module("astrbot.api.star", StarTools=object, Context=object)
    _stub_module(
        "astrbot.api.message_components",
        Image=FakeImage,
        Plain=object,
        At=object,
        Reply=object,
        Face=object,
        Poke=object,
        Record=object,
        Video=object,
        Json=object,
        Node=object,
        Nodes=object,
        Forward=object,
        File=object,
    )
    _stub_module("astrbot.core", AstrBotConfig=object)
    _stub_module("astrbot.core.message")
    _stub_module("astrbot.core.message.components", BaseMessageComponent=object)
    _stub_module(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_data_path=lambda: "/tmp/fake_astrbot_data",
        get_astrbot_plugin_data_path=lambda: "/tmp/fake_astrbot_plugin_data",
    )

from core.llm.preset_prompts import (  # noqa: E402
    FeatureKey,
    build_xml_instructions,
    get_decision_rules,
)
from core.llm.xml_parse import XmlParse  # noqa: E402
from core.utils.meme_manager_client import MemeManagerClient  # noqa: E402


class MemeManagerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.memes_dir = self.temp_dir / "memes"
        self.memes_dir.mkdir(parents=True)
        self.db_path = self.temp_dir / "memes.db"

        # 初始化测试数据库
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE memes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT UNIQUE,
            emotions TEXT,
            personas TEXT,
            original_hash TEXT,
            description TEXT,
            send_mode TEXT DEFAULT 'sticker'
        )
        """)

        # 插入测试数据
        # 1. 通用表情包 (personas = '*')
        (self.memes_dir / "cat_happy.png").write_text("fake_cat")
        cursor.execute(
            "INSERT INTO memes (id, filename, emotions, personas, description, send_mode) VALUES (?, ?, ?, ?, ?, ?)",
            (1, "cat_happy.png", "开心,猫猫,卖萌", "*", "一只开心的猫咪在地上打滚", "sticker"),
        )

        # 2. 人设专属表情包 (personas = ',alice,')
        (self.memes_dir / "alice_angry.gif").write_text("fake_alice")
        cursor.execute(
            "INSERT INTO memes (id, filename, emotions, personas, description, send_mode) VALUES (?, ?, ?, ?, ?, ?)",
            (2, "alice_angry.gif", "傲娇,生气,哼", ",alice,", "爱丽丝双手抱胸别过头哼了一声", "sticker"),
        )

        # 3. 另一个人设专属表情包 (personas = ',bob,')
        (self.memes_dir / "bob_laugh.png").write_text("fake_bob")
        cursor.execute(
            "INSERT INTO memes (id, filename, emotions, personas, description, send_mode) VALUES (?, ?, ?, ?, ?, ?)",
            (3, "bob_laugh.png", "爆笑,大笑", ",bob,", "鲍勃拍桌大笑", "image"),
        )

        conn.commit()
        conn.close()

        self.client = MemeManagerClient(plugin_data_dir=self.temp_dir)

    def test_client_get_candidate_memes_persona_filtering(self):
        # Alice 只能获取通用的 (id=1) 和 Alice 专属的 (id=2)，不能获取 Bob 专属的 (id=3)
        alice_memes = asyncio.run(self.client.get_candidate_memes(persona_id="alice", query="傲娇", count=10))
        alice_ids = [m["id"] for m in alice_memes]
        self.assertIn(2, alice_ids)
        self.assertIn(1, alice_ids)
        self.assertNotIn(3, alice_ids)

        # 评分排在最前面的应该是匹配到 "傲娇" 的 id=2
        self.assertEqual(alice_memes[0]["id"], 2)

    def test_client_get_candidate_memes_scoring(self):
        # 搜索 "开心" -> id=1 (开心) 得分最高 (命中第1个标签 1000 分)
        memes = asyncio.run(self.client.get_candidate_memes(persona_id="alice", query="开心,打滚", count=5))
        self.assertEqual(memes[0]["id"], 1)
        self.assertTrue(memes[0]["score"] >= 1000)

    def test_client_get_meme_by_id(self):
        meme = self.client.get_meme_by_id(2)
        self.assertIsNotNone(meme)
        self.assertEqual(meme["filename"], "alice_angry.gif")
        self.assertEqual(meme["send_mode"], "sticker")

        none_meme = self.client.get_meme_by_id(999)
        self.assertIsNone(none_meme)

    def test_client_build_meme_component(self):
        # 1. sticker 模式 (sub_type = 1)
        meme = self.client.get_meme_by_id(2)
        comp = self.client.build_meme_component(meme)
        comp_file = str(comp.file).removeprefix("file://")
        self.assertEqual(comp_file, str((self.memes_dir / "alice_angry.gif").resolve()))
        self.assertEqual(comp.sub_type, 1)
        self.assertIn("爱丽丝", comp.meme_desc)

        # 2. image 原图模式 (sub_type = 0)
        meme_img = self.client.get_meme_by_id(3)
        comp_img = self.client.build_meme_component(meme_img)
        self.assertEqual(comp_img.sub_type, 0)

    def test_client_format_candidates_for_prompt(self):
        candidates = [
            {"id": 1, "description": "开心的猫咪", "emotions": "开心, 猫猫"},
            {"id": 2, "description": "爱丽丝生气", "emotions": "傲娇, 生气"},
        ]
        formatted = MemeManagerClient.format_candidates_for_prompt(candidates)
        self.assertIn("[开心的猫咪](sticker_id: mm_1) - 标签: 开心, 猫猫", formatted)
        self.assertIn("[爱丽丝生气](sticker_id: mm_2) - 标签: 傲娇, 生气", formatted)
        self.assertEqual(MemeManagerClient.format_candidates_for_prompt([]), "")

    def test_decision_rules_conditional_generation(self):
        rules_enabled = get_decision_rules(use_meme_manager=True)
        self.assertIn('meme_tags="string"', rules_enabled)
        self.assertIn("表情包意图预测", rules_enabled)

        rules_disabled = get_decision_rules(use_meme_manager=False)
        self.assertNotIn('meme_tags="string"', rules_disabled)
        self.assertNotIn("表情包意图预测", rules_disabled)

    def test_build_xml_instructions_conditional_add_sticker(self):
        # 当 use_meme_manager=True 时，不应包含 <add_sticker>，只保留 <sticker>
        inst_enabled = build_xml_instructions(
            enabled_features=[FeatureKey.STICKER],
            use_meme_manager=True,
        )
        self.assertIn("<sticker", inst_enabled)
        self.assertNotIn("<add_sticker", inst_enabled)

        # 当 use_meme_manager=False 时，包含发送与收集
        inst_disabled = build_xml_instructions(
            enabled_features=[FeatureKey.STICKER],
            use_meme_manager=False,
        )
        self.assertIn("<sticker", inst_disabled)
        self.assertIn("<add_sticker", inst_disabled)

    def test_decode_decision_xml_with_meme_tags(self):
        xml_str = """
        <think>用户在开玩笑，可以用傲娇表情包互动</think>
        <decision reply="true" use_rag="false" rag_query="" meme_tags="傲娇, 哼"/>
        """
        parser = XmlParse(data_cache=None, emoji_manager=None, meme_manager_client=self.client)
        decision = parser.decode_decision_xml(xml_str)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.reply_decision, 1)
        self.assertEqual(decision.use_rag, 0)
        self.assertEqual(decision.meme_tags, "傲娇, 哼")

    def test_xml_parse_load_sticker_image_mm_id(self):
        parser = XmlParse(data_cache=None, emoji_manager=None, meme_manager_client=self.client)

        # 异步测试 _load_sticker_image
        async def run():
            img = await parser._load_sticker_image("mm_2")
            self.assertIsNotNone(img)
            img_file = str(img.file).removeprefix("file://")
            self.assertEqual(img_file, str((self.memes_dir / "alice_angry.gif").resolve()))
            self.assertEqual(img.sub_type, 1)

            img_colon = await parser._load_sticker_image("mm:1")
            self.assertIsNotNone(img_colon)
            img_colon_file = str(img_colon.file).removeprefix("file://")
            self.assertEqual(img_colon_file, str((self.memes_dir / "cat_happy.png").resolve()))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
