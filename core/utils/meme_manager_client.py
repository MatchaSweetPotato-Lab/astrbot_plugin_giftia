import random
import re
import sqlite3
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Image
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)


class MemeManagerClient:
    """Giftia 端跨插件客户端：负责与 meme_manager 插件进行委托调用与数据通信。"""

    def __init__(
        self,
        plugin_data_dir: Path | None = None,
        context=None,
    ):
        self.context = context
        self.plugin_data_dir = plugin_data_dir or self._locate_plugin_data_dir()
        self.db_path = self.plugin_data_dir / "memes.db"
        self.memes_dir = self.plugin_data_dir / "memes"

    def _get_meme_manager_star(self):
        """获取已加载的 meme_manager 插件 Star 实例"""
        sources = []
        if self.context and hasattr(self.context, "get_all_stars"):
            try:
                sources.extend(self.context.get_all_stars())
            except Exception:
                pass

        try:
            from astrbot.core.star.star import star_registry

            sources.extend(star_registry)
        except Exception:
            pass

        for meta in sources:
            cls_obj = getattr(meta, "star_cls", None)
            name_str = str(getattr(meta, "name", "") or "").lower()
            dir_str = str(getattr(meta, "root_dir_name", "") or "").lower()
            if ("meme_manager" in name_str or "meme_manager" in dir_str) and cls_obj:
                return cls_obj

        return None

    @staticmethod
    def _locate_plugin_data_dir() -> Path:
        """定位 meme_manager 的数据目录。若找不到则抛出明确异常。"""
        candidates = []

        try:
            candidates.append(Path(get_astrbot_plugin_data_path()) / "meme_manager")
        except Exception:
            pass

        try:
            candidates.append(Path(get_astrbot_data_path()) / "plugin_data" / "meme_manager")
            candidates.append(Path(get_astrbot_data_path()) / "memes_data")
        except Exception:
            pass

        # 尝试相对于运行路径及插件目录
        cwd = Path.cwd()
        candidates.extend([
            cwd / "data" / "plugin_data" / "meme_manager",
            cwd / "data" / "plugins" / "astrbot_plugin_meme_manager" / "data" / "plugin_data" / "meme_manager",
            cwd / "data" / "memes_data",
        ])

        for path in candidates:
            if (path / "memes.db").is_file():
                return path.resolve()

        # 如果都不存在，默认使用规范路径，供后续抛出明确错误
        try:
            return (Path(get_astrbot_plugin_data_path()) / "meme_manager").resolve()
        except Exception:
            return (cwd / "data" / "plugin_data" / "meme_manager").resolve()

    def check_installed(self) -> None:
        """检查 meme_manager 数据库和表情包目录是否存在，不存在则抛出清晰异常。"""
        if not self.db_path.is_file():
            raise RuntimeError(
                f"[Giftia] 开启了 use_meme_manager，但在路径 {self.db_path} 未找到 memes.db。"
                "请确认 astrbot_plugin_meme_manager 插件已正确安装且已生成表情包数据。"
            )
        if not self.memes_dir.is_dir():
            raise RuntimeError(
                f"[Giftia] 开启了 use_meme_manager，但在路径 {self.memes_dir} 未找到 memes 表情包目录。"
            )

    def _get_conn(self) -> sqlite3.Connection:
        self.check_installed()
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    async def get_candidate_memes(
        self,
        persona_id: str = "default",
        query: str | list[str] = "",
        count: int = 5,
        event=None,
    ) -> list[dict]:
        """
        向 meme_manager 请求人设与检索词匹配打分后的表情包候选列表。
        职责完全交由 meme_manager 负责。
        """
        if count <= 0:
            return []

        query_str = query if isinstance(query, str) else ", ".join([str(t) for t in query if t])

        # 1. 优先调用 meme_manager 实例的统一接口
        mm_star = self._get_meme_manager_star()
        if mm_star and hasattr(mm_star, "get_candidate_memes"):
            try:
                res = await mm_star.get_candidate_memes(
                    persona_id=persona_id,
                    query=query_str,
                    count=count,
                    event=event,
                )
                if res:
                    return res
            except Exception as e:
                logger.debug(f"[Giftia] 调用 meme_manager get_candidate_memes 失败，降级本地查询: {e}")

        # 2. 本地回退（主要用于独立测试环境或 meme_manager 实例尚未就绪）
        raw_tags = [t.strip().lower() for t in re.split(r"[,，\s]+", query_str) if t.strip()]
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, filename, emotions, personas, description, send_mode "
                "FROM memes WHERE (personas = '*' OR ',' || personas || ',' LIKE ?)",
                (f"%,{persona_id},%",),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        if not rows:
            return []

        candidates = []
        for row in rows:
            fn = str(row["filename"])
            if not (self.memes_dir / fn).is_file():
                continue
            emotions_str = str(row["emotions"] or "")
            desc_str = str(row["description"] or "")
            emotions_list = [e.strip().lower() for e in emotions_str.split(",") if e.strip()]

            score = 0
            if raw_tags:
                for idx, tag in enumerate(raw_tags):
                    tag_weight = 1000 if idx == 0 else max(1, 400 // (2 ** (idx - 1)))
                    if tag in emotions_list:
                        score += tag_weight
                    elif any(tag in emo for emo in emotions_list):
                        score += int(tag_weight * 0.8)
                    elif tag in desc_str.lower():
                        score += int(tag_weight * 0.5)

            candidates.append({
                "id": int(row["id"]),
                "filename": fn,
                "emotions": emotions_str,
                "description": desc_str,
                "send_mode": str(row["send_mode"] or "sticker"),
                "score": score,
            })

        random.shuffle(candidates)
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:count]

    def get_meme_by_id(self, meme_id: int | str) -> dict | None:
        """通过自增 ID 获取单条表情包信息（优先委托给 meme_manager）。"""
        mm_star = self._get_meme_manager_star()
        if mm_star and hasattr(mm_star, "get_meme_by_id"):
            try:
                res = mm_star.get_meme_by_id(meme_id)
                if res:
                    return res
            except Exception:
                pass

        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, filename, emotions, personas, description, send_mode "
                "FROM memes WHERE id = ?",
                (int(meme_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return {
                "id": int(row["id"]),
                "filename": str(row["filename"]),
                "emotions": str(row["emotions"] or ""),
                "description": str(row["description"] or ""),
                "personas": str(row["personas"] or "*"),
                "send_mode": str(row["send_mode"] or "sticker"),
            }
        finally:
            conn.close()

    def build_meme_component(self, meme: dict | int | str) -> Image | None:
        """构建 Image 消息组件（优先委托给 meme_manager）。"""
        mm_star = self._get_meme_manager_star()
        if mm_star and hasattr(mm_star, "build_meme_component"):
            try:
                comp = mm_star.build_meme_component(meme)
                if comp:
                    return comp
            except Exception:
                pass

        if isinstance(meme, (int, str)) and str(meme).isdigit():
            meme = self.get_meme_by_id(int(meme))

        if not meme or not isinstance(meme, dict):
            return None

        filename = meme.get("filename")
        if not filename:
            return None

        image_path = (self.memes_dir / filename).resolve()
        if not image_path.is_file():
            return None

        comp = Image.fromFileSystem(str(image_path))
        send_mode = meme.get("send_mode") or "sticker"
        if send_mode == "sticker":
            object.__setattr__(comp, "sub_type", 1)
        else:
            object.__setattr__(comp, "sub_type", 0)
        desc = meme.get("description") or meme.get("emotions") or "表情包"
        object.__setattr__(comp, "meme_desc", desc)
        return comp

    @staticmethod
    def format_candidates_for_prompt(candidates: list[dict]) -> str:
        """将表情包候选列表格式化为适合注入 LLM 回复提示词的 Markdown 列表文本。"""
        if not candidates:
            return ""
        result_lines = []
        for c in candidates:
            desc = c.get("description") or c.get("emotions") or "表情包"
            emotions_str = c.get("emotions") or "无"
            result_lines.append(f"[{desc}](sticker_id: mm_{c['id']}) - 标签: {emotions_str}")
        return "\n".join(result_lines)
