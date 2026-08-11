from __future__ import annotations

import json
import os

from astrbot.api import logger
from astrbot.api.star import StarTools
from ..utils.schemas import FeatureKey

INTERACTIVE_FEATURES_METADATA = [
    {"key": FeatureKey.POKE, "label": "戳一戳", "note": "仅OneBot"},
    {"key": FeatureKey.EMOJI_LIKE, "label": "贴表情回应", "note": "仅OneBot"},
    {"key": FeatureKey.REPEAT, "label": "消息复读", "note": "仅OneBot"},
    {"key": FeatureKey.LIKE, "label": "点赞名片", "note": "仅OneBot"},
    {"key": FeatureKey.DELETE, "label": "撤回自身消息", "note": "仅OneBot"},
    {"key": FeatureKey.GROUP_ADMIN, "label": "群管禁言/踢人", "note": "仅OneBot"},
    {"key": FeatureKey.SCHEDULE_TASK, "label": "设置/查看/删除定时任务", "note": ""},
    {"key": FeatureKey.TASK_BOARD, "label": "短期任务看板", "note": ""},
    {"key": FeatureKey.STICKER, "label": "表情包发送与收集", "note": ""},
    {"key": FeatureKey.MEMORY_QUERY_DELETE, "label": "记忆查询与删除", "note": ""},
    {"key": FeatureKey.RECAPTION, "label": "重新转述媒体", "note": ""},
    {"key": FeatureKey.SET_CALL_NAME, "label": "设置/修改用户称呼", "note": ""},
    {"key": FeatureKey.LEAVE, "label": "主动退群", "note": "仅OneBot"},
]

DEFAULT_INTERACTIVE_FEATURES = [
    item["key"] for item in INTERACTIVE_FEATURES_METADATA if item["key"] != FeatureKey.LEAVE
]

DEFAULT_BOT_CONFIG = {
    "enabled": True,
    "name": "Giftia",
    "nickname": "Giftia",
    "adapter_ids": [],
    "decision_conf": {
        "enabled": True,
        "provider_ids": [],
        "group_whitelist": [],
        "decision_prompt": "",
        "reply_active_window": 10,
        "proactive_probability": 0,
        "keyword_trigger_enabled": False,
        "keyword_rules": [],
        "keyword_default_probability": 100,
    },
    "llm_reply_conf": {
        "enabled": True,
        "provider_ids": [],
        "provider_selection_mode": "fallback",
        "llm_reply_prompt": "",
    },
    "tts_config": {
        "enabled": False,
        "provider_type": "minimax",
        "zh_provider_id": "",
        "en_provider_id": "",
        "ja_provider_id": "",
        "default_language": "中文",
        "replace_in_message": False,
        "signature_voices": [],
    },
    "enabled_interactive_features": DEFAULT_INTERACTIVE_FEATURES,
}


class BotConfigManager:
    def __init__(self, plugin):
        self.plugin = plugin
        try:
            self.data_dir = str(StarTools.get_data_dir("astrbot_plugin_giftia"))
        except Exception:
            self.data_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.config_file = os.path.join(self.data_dir, "bots_config.json")
        self.bots: list[dict] = []

    def load_bots(self) -> list[dict]:
        """加载机器人配置列表，若文件不存在则初始化默认配置。"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.bots = [self.normalize_bot_config(b) for b in data if isinstance(b, dict)]
                    logger.info(f"[Giftia BotConfigManager] 从 {self.config_file} 加载了 {len(self.bots)} 个机器人配置")
                    return self.bots
            except Exception as e:
                logger.error(f"[Giftia BotConfigManager] 读取 {self.config_file} 失败: {e}")

        # 初始化默认机器人配置
        logger.info("[Giftia BotConfigManager] 未找到已有机器人配置，自动初始化默认机器人 Giftia")
        self.bots = [dict(DEFAULT_BOT_CONFIG)]
        self.save_bots(self.bots)
        return self.bots

    def save_bots(self, bots: list[dict]) -> bool:
        """保存机器人配置列表到 data/bots_config.json。"""
        normalized_bots = [self.normalize_bot_config(b) for b in bots if isinstance(b, dict)]
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(normalized_bots, f, ensure_ascii=False, indent=2)
            self.bots = normalized_bots
            logger.info(f"[Giftia BotConfigManager] 已保存 {len(normalized_bots)} 个机器人配置到 {self.config_file}")
            return True
        except Exception as e:
            logger.error(f"[Giftia BotConfigManager] 保存 {self.config_file} 失败: {e}")
            return False

    @staticmethod
    def format_signature_voice_item(v: str | dict) -> str:
        """将单条标志性语音配置规范化为 '音频: 触发词' 的标准字符串。"""
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, dict):
            audios = v.get("audio")
            if isinstance(audios, list):
                audio_str = ", ".join([str(a).strip() for a in audios if a])
            else:
                audio_str = str(audios or "").strip()

            texts = ", ".join([str(t).strip() for t in (v.get("matched_texts") or []) if t])
            if audio_str:
                return f"{audio_str}: {texts}" if texts else audio_str
        return ""

    def normalize_bot_config(self, raw_bot: dict) -> dict:
        """补充和归一化单个机器人配置项。"""
        bot = dict(raw_bot or {})
        bot["enabled"] = bool(bot.get("enabled", True))
        bot["name"] = str(bot.get("name") or "Giftia").strip()
        bot["nickname"] = str(bot.get("nickname") or bot["name"]).strip()
        bot["adapter_ids"] = [str(a).strip() for a in bot.get("adapter_ids") or [] if a]

        # decision_conf
        raw_dec = bot.get("decision_conf") or {}
        bot["decision_conf"] = {
            "enabled": bool(raw_dec.get("enabled", True)),
            "provider_ids": [str(p).strip() for p in raw_dec.get("provider_ids") or [] if p],
            "group_whitelist": [str(g).strip() for g in raw_dec.get("group_whitelist") or [] if g],
            "decision_prompt": str(raw_dec.get("decision_prompt") or ""),
            "reply_active_window": int(raw_dec.get("reply_active_window", 10)),
            "proactive_probability": int(raw_dec.get("proactive_probability", 0)),
            "keyword_trigger_enabled": bool(raw_dec.get("keyword_trigger_enabled", False)),
            "keyword_rules": [str(k).strip() for k in raw_dec.get("keyword_rules") or [] if k],
            "keyword_default_probability": int(raw_dec.get("keyword_default_probability", 100)),
        }

        # llm_reply_conf
        raw_reply = bot.get("llm_reply_conf") or {}
        bot["llm_reply_conf"] = {
            "enabled": bool(raw_reply.get("enabled", True)),
            "provider_ids": [str(p).strip() for p in raw_reply.get("provider_ids") or [] if p],
            "provider_selection_mode": str(raw_reply.get("provider_selection_mode") or "fallback"),
            "llm_reply_prompt": str(raw_reply.get("llm_reply_prompt") or ""),
        }

        # tts_config
        raw_tts = bot.get("tts_config") or {}
        signature_voices = raw_tts.get("signature_voices") or []
        formatted_voices = []
        if isinstance(signature_voices, list):
            for v in signature_voices:
                formatted = self.format_signature_voice_item(v)
                if formatted:
                    formatted_voices.append(formatted)

        # language_provider_map
        raw_lang_map = raw_tts.get("language_provider_map")
        lang_map = []
        if isinstance(raw_lang_map, list):
            for item in raw_lang_map:
                if isinstance(item, dict):
                    lang = str(item.get("language") or item.get("lang") or "").strip()
                    p_id = str(item.get("provider_id") or item.get("provider") or "").strip()
                    if lang and p_id:
                        lang_map.append({"language": lang, "provider_id": p_id})

        # Fallback convert legacy fields if language_provider_map is empty
        if not lang_map:
            legacy_defaults = [("中文", "zh_provider_id"), ("英文", "en_provider_id"), ("日文", "ja_provider_id")]
            default_lang = str(raw_tts.get("default_language") or "中文").strip()
            if default_lang:
                legacy_defaults.sort(key=lambda x: 0 if x[0] == default_lang else 1)
            for lang_name, key in legacy_defaults:
                pid = str(raw_tts.get(key) or "").strip()
                if pid:
                    lang_map.append({"language": lang_name, "provider_id": pid})

        bot["tts_config"] = {
            "enabled": bool(raw_tts.get("enabled", False)),
            "provider_type": str(raw_tts.get("provider_type") or "minimax").strip().lower(),
            "language_provider_map": lang_map,
            "replace_in_message": bool(raw_tts.get("replace_in_message", False)),
            "signature_voices": formatted_voices,
        }



        # enabled_interactive_features
        features = bot.get("enabled_interactive_features")
        if features is None or not isinstance(features, list):
            bot["enabled_interactive_features"] = list(DEFAULT_INTERACTIVE_FEATURES)
        else:
            normalized_features = []
            for f in features:
                if not f:
                    continue
                s = str(f).strip()
                key = s.split("(")[0].strip() if "(" in s else s
                if key and key not in normalized_features:
                    normalized_features.append(key)
            bot["enabled_interactive_features"] = normalized_features

        return bot
