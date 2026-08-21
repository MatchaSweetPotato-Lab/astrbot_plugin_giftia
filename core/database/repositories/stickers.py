import json
from datetime import datetime
import aiosqlite
from .base import BaseRepository
from ...utils.schemas import Sticker

class StickersRepository(BaseRepository):
    async def insert_sticker(
        self,
        sticker_id: str,
        name: str,
        category: str,
        tags: list[str],
        description: str,
        filename: str = "",
    ):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tags_json = json.dumps(tags, ensure_ascii=False)
        await self.conn.execute(
            """
            INSERT INTO stickers (sticker_id, name, category, tags, description, filename, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sticker_id) DO UPDATE SET
                name=excluded.name,
                category=excluded.category,
                tags=excluded.tags,
                description=excluded.description,
                filename=excluded.filename,
                updated_at=excluded.updated_at
            """,
            (
                sticker_id,
                name,
                category,
                tags_json,
                description,
                filename,
                now_str,
                now_str,
            ),
        )
        await self.conn.commit()

    async def update_sticker(
        self,
        sticker_id: str,
        name: str,
        category: str,
        tags: list[str],
        description: str,
    ) -> bool:
        """仅更新表情包元数据，不触碰 filename。

        与 insert_sticker 的区别：insert_sticker 是全字段 upsert 且 filename 默认为空串，
        用它改元数据会把已有的图片文件名清空，导致 get_sticker_path() 返回 None、
        机器人再也发不出这张表情包。Web 端编辑必须走这个方法。
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tags_json = json.dumps(tags, ensure_ascii=False)
        cursor = await self.conn.execute(
            """
            UPDATE stickers
            SET name = ?, category = ?, tags = ?, description = ?, updated_at = ?
            WHERE sticker_id = ?
            """,
            (name, category, tags_json, description, now_str, sticker_id),
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_sticker(row) -> Sticker:
        """把一行数据库记录转换为 Sticker 对象"""
        try:
            tags = json.loads(row["tags"]) if row["tags"] else []
        except json.JSONDecodeError:
            tags = []
        if not isinstance(tags, list):
            tags = []

        return Sticker(
            sticker_id=row["sticker_id"],
            name=row["name"] or "",
            category=row["category"] or "",
            tags=[str(t) for t in tags],
            description=row["description"] or "",
            filename=row["filename"] or "",
        )

    async def get_sticker(self) -> list[Sticker]:
        """获取全部表情包数据"""
        async with self.conn.execute(
            "SELECT sticker_id, name, category, tags, description, filename FROM stickers"
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_sticker(row) for row in rows]

    async def get_sticker_by_id(self, sticker_id: str) -> Sticker | None:
        """按 sticker_id 获取单个表情包"""
        async with self.conn.execute(
            """
            SELECT sticker_id, name, category, tags, description, filename
            FROM stickers WHERE sticker_id = ?
            """,
            (sticker_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_sticker(row) if row else None

    async def query_stickers(
        self,
        page: int = 1,
        limit: int = 12,
        category: str = "",
        tag: str = "",
        search: str = "",
        sticker_ids: list[str] | None = None,
        sort: str = "created_desc",
    ) -> tuple[list[dict], int]:
        """分页查询表情包，返回 (记录列表, 总数)。

        Args:
            sticker_ids: 若不为 None，则只在这批 ID 内查询（用于按机器人归属筛选）。
                传入空列表表示结果必然为空。
        """
        conditions: list[str] = []
        params: list = []

        if category:
            conditions.append("category = ?")
            params.append(category)

        if tag:
            # tags 存的是 JSON 数组字符串，按带引号的完整词匹配，
            # 避免搜 "可爱" 时误命中 "不可爱"
            conditions.append("tags LIKE ?")
            params.append(f'%"{tag}"%')

        if search:
            conditions.append(
                "(name LIKE ? OR description LIKE ? OR category LIKE ? OR sticker_id LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like, like])

        if sticker_ids is not None:
            if not sticker_ids:
                return [], 0
            placeholders = ",".join(["?"] * len(sticker_ids))
            conditions.append(f"sticker_id IN ({placeholders})")
            params.extend(sticker_ids)

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        order_map = {
            "created_desc": "created_at DESC",
            "created_asc": "created_at ASC",
            "updated_desc": "updated_at DESC",
            "name_asc": "name ASC",
        }
        order_by = order_map.get(sort, "created_at DESC")

        count_sql = f"SELECT COUNT(*) AS total FROM stickers {where_clause}"
        async with self.conn.execute(count_sql, params) as cursor:
            row = await cursor.fetchone()
            total = row["total"] if row else 0

        offset = max(0, (max(1, page) - 1) * limit)
        data_sql = f"""
            SELECT sticker_id, name, category, tags, description, filename,
                   created_at, updated_at
            FROM stickers
            {where_clause}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
        """
        async with self.conn.execute(data_sql, params + [limit, offset]) as cursor:
            rows = await cursor.fetchall()

        items: list[dict] = []
        for row in rows:
            sticker = self._row_to_sticker(row)
            items.append(
                {
                    "sticker_id": sticker.sticker_id,
                    "name": sticker.name,
                    "category": sticker.category,
                    "tags": sticker.tags,
                    "description": sticker.description,
                    "filename": sticker.filename,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return items, total

    async def delete_sticker(self, sticker_id: str):
        """删除表情包数据"""
        await self.conn.execute(
            """
            DELETE FROM stickers WHERE sticker_id = ?
            """,
            (sticker_id,),
        )
        await self.conn.commit()

    async def insert_sticker_bot(self, sticker_id: str, bot_name: str):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self.conn.execute(
            """
            SELECT sticker_ids FROM stickers_bot WHERE bot_name = ?
            """,
            (bot_name,),
        ) as cursor:
            row = await cursor.fetchone()
        if row and row["sticker_ids"]:
            sticker_ids = set(json.loads(row["sticker_ids"]))
        else:
            sticker_ids = set()
        sticker_ids.add(sticker_id)
        sticker_ids_json = json.dumps(list(sticker_ids), ensure_ascii=False)
        await self.conn.execute(
            """
            INSERT INTO stickers_bot (bot_name, sticker_ids, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(bot_name) DO UPDATE SET
                sticker_ids=excluded.sticker_ids,
                updated_at=excluded.updated_at
            """,
            (
                bot_name,
                sticker_ids_json,
                now_str,
                now_str,
            ),
        )
        await self.conn.commit()

    async def get_sticker_categories(self) -> list[str]:
        """获取所有已知的表情包分类"""
        cursor = await self.conn.execute(
            "SELECT DISTINCT category FROM stickers WHERE category IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows if row[0]]

    async def get_sticker_bot(self, bot_name: str) -> list[str]:
        """获取机器人表情包列表"""
        async with self.conn.execute(
            """
            SELECT sticker_ids FROM stickers_bot WHERE bot_name = ?
            """,
            (bot_name,),
        ) as cursor:
            row = await cursor.fetchone()
        if row and row["sticker_ids"]:
            return json.loads(row["sticker_ids"])
        return []

    # ── 归属管理 ────────────────────────────────────────────────────────

    async def delete_sticker_bot(self, sticker_id: str, bot_name: str) -> bool:
        """把单个表情包从指定机器人的收藏列表里移除"""
        async with self.conn.execute(
            "SELECT sticker_ids FROM stickers_bot WHERE bot_name = ?",
            (bot_name,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row or not row["sticker_ids"]:
            return False

        try:
            sticker_ids = json.loads(row["sticker_ids"])
        except json.JSONDecodeError:
            return False
        if not isinstance(sticker_ids, list) or sticker_id not in sticker_ids:
            return False

        sticker_ids = [sid for sid in sticker_ids if sid != sticker_id]
        await self.conn.execute(
            "UPDATE stickers_bot SET sticker_ids = ?, updated_at = ? WHERE bot_name = ?",
            (
                json.dumps(sticker_ids, ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                bot_name,
            ),
        )
        await self.conn.commit()
        return True

    async def remove_sticker_from_all_bots(self, sticker_id: str) -> list[str]:
        """把表情包从所有机器人的收藏列表里移除，返回受影响的 bot_name 列表。

        删除表情包时必须调用，否则 stickers_bot 里会残留失效 ID。
        """
        async with self.conn.execute(
            "SELECT bot_name, sticker_ids FROM stickers_bot"
        ) as cursor:
            rows = await cursor.fetchall()

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        affected: list[str] = []
        for row in rows:
            if not row["sticker_ids"]:
                continue
            try:
                sticker_ids = json.loads(row["sticker_ids"])
            except json.JSONDecodeError:
                continue
            if not isinstance(sticker_ids, list) or sticker_id not in sticker_ids:
                continue

            sticker_ids = [sid for sid in sticker_ids if sid != sticker_id]
            await self.conn.execute(
                "UPDATE stickers_bot SET sticker_ids = ?, updated_at = ? WHERE bot_name = ?",
                (
                    json.dumps(sticker_ids, ensure_ascii=False),
                    now_str,
                    row["bot_name"],
                ),
            )
            affected.append(row["bot_name"])

        if affected:
            await self.conn.commit()
        return affected

    async def get_all_bot_sticker_map(self) -> dict[str, list[str]]:
        """一次读全部 stickers_bot 反向索引出 {sticker_id: [bot_name, ...]}。

        列表接口用它批量标注归属，避免逐条查询造成 N+1。
        """
        async with self.conn.execute(
            "SELECT bot_name, sticker_ids FROM stickers_bot"
        ) as cursor:
            rows = await cursor.fetchall()

        mapping: dict[str, list[str]] = {}
        for row in rows:
            if not row["sticker_ids"]:
                continue
            try:
                sticker_ids = json.loads(row["sticker_ids"])
            except json.JSONDecodeError:
                continue
            if not isinstance(sticker_ids, list):
                continue
            for sid in sticker_ids:
                mapping.setdefault(str(sid), []).append(row["bot_name"])

        for bot_names in mapping.values():
            bot_names.sort()
        return mapping

    # ── 分类与标签聚合 ──────────────────────────────────────────────────

    async def get_all_sticker_tags(self) -> list[str]:
        """获取所有去重后的标签，按名称排序"""
        async with self.conn.execute(
            "SELECT tags FROM stickers WHERE tags IS NOT NULL AND tags != ''"
        ) as cursor:
            rows = await cursor.fetchall()

        tags: set[str] = set()
        for row in rows:
            try:
                parsed = json.loads(row["tags"])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                for tag in parsed:
                    text = str(tag).strip()
                    if text:
                        tags.add(text)
        return sorted(tags)

    async def get_category_stats(self) -> list[dict]:
        """获取分类及其表情包数量，数量降序"""
        async with self.conn.execute(
            """
            SELECT COALESCE(NULLIF(category, ''), '') AS category, COUNT(*) AS count
            FROM stickers
            GROUP BY COALESCE(NULLIF(category, ''), '')
            ORDER BY count DESC
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"category": row["category"] or "", "count": row["count"]} for row in rows]

    async def get_tag_stats(self) -> list[dict]:
        """获取标签及其表情包数量，数量降序。tags 是 JSON 数组，只能在 Python 侧聚合。"""
        async with self.conn.execute(
            "SELECT tags FROM stickers WHERE tags IS NOT NULL AND tags != ''"
        ) as cursor:
            rows = await cursor.fetchall()

        counter: dict[str, int] = {}
        for row in rows:
            try:
                parsed = json.loads(row["tags"])
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, list):
                continue
            # 同一张表情包内的重复标签只算一次
            for tag in {str(t).strip() for t in parsed if str(t).strip()}:
                counter[tag] = counter.get(tag, 0) + 1

        return [
            {"tag": tag, "count": count}
            for tag, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    # ── 批量与重命名 ────────────────────────────────────────────────────

    async def rename_category(self, old_name: str, new_name: str) -> int:
        """重命名分类；若 new_name 已存在则等价于合并。返回受影响行数。"""
        if old_name:
            cursor = await self.conn.execute(
                "UPDATE stickers SET category = ?, updated_at = ? WHERE category = ?",
                (new_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), old_name),
            )
        else:
            # 空分类（NULL 或空串）统一归入新分类
            cursor = await self.conn.execute(
                """
                UPDATE stickers SET category = ?, updated_at = ?
                WHERE category IS NULL OR category = ''
                """,
                (new_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        await self.conn.commit()
        return cursor.rowcount

    async def _rewrite_tags(
        self, transform, where_sql: str = "", where_params: tuple = ()
    ) -> int:
        """通用标签重写：对每条记录的 tags 列表应用 transform，有变化才写回。

        Args:
            transform: 接收 list[str] 返回 list[str] 的函数。
            where_sql: 附加的 WHERE 子句（不含 WHERE 关键字），为空则扫描全表。
        """
        sql = "SELECT sticker_id, tags FROM stickers"
        if where_sql:
            sql += f" WHERE {where_sql}"
        async with self.conn.execute(sql, where_params) as cursor:
            rows = await cursor.fetchall()

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changed = 0
        for row in rows:
            try:
                current = json.loads(row["tags"]) if row["tags"] else []
            except json.JSONDecodeError:
                current = []
            if not isinstance(current, list):
                current = []
            current = [str(t) for t in current]

            updated = transform(current)
            if updated == current:
                continue

            await self.conn.execute(
                "UPDATE stickers SET tags = ?, updated_at = ? WHERE sticker_id = ?",
                (
                    json.dumps(updated, ensure_ascii=False),
                    now_str,
                    row["sticker_id"],
                ),
            )
            changed += 1

        if changed:
            await self.conn.commit()
        return changed

    async def rename_tag(self, old_tag: str, new_tag: str) -> int:
        """重命名标签；若 new_tag 已存在于同一张表情包则自动去重（即合并）。"""

        def transform(tags: list[str]) -> list[str]:
            if old_tag not in tags:
                return tags
            result: list[str] = []
            for tag in tags:
                candidate = new_tag if tag == old_tag else tag
                if candidate not in result:
                    result.append(candidate)
            return result

        return await self._rewrite_tags(transform, "tags LIKE ?", (f'%"{old_tag}"%',))

    async def delete_tag(self, tag: str) -> int:
        """从所有表情包上移除某个标签"""

        def transform(tags: list[str]) -> list[str]:
            return [t for t in tags if t != tag]

        return await self._rewrite_tags(transform, "tags LIKE ?", (f'%"{tag}"%',))

    async def batch_update_category(
        self, sticker_ids: list[str], category: str
    ) -> int:
        """批量设置分类"""
        if not sticker_ids:
            return 0
        placeholders = ",".join(["?"] * len(sticker_ids))
        cursor = await self.conn.execute(
            f"""
            UPDATE stickers SET category = ?, updated_at = ?
            WHERE sticker_id IN ({placeholders})
            """,
            [category, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *sticker_ids],
        )
        await self.conn.commit()
        return cursor.rowcount

    async def batch_add_tags(self, sticker_ids: list[str], tags: list[str]) -> int:
        """批量追加标签（已有的不重复添加）"""
        if not sticker_ids or not tags:
            return 0

        def transform(current: list[str]) -> list[str]:
            result = list(current)
            for tag in tags:
                if tag not in result:
                    result.append(tag)
            return result

        placeholders = ",".join(["?"] * len(sticker_ids))
        return await self._rewrite_tags(
            transform, f"sticker_id IN ({placeholders})", tuple(sticker_ids)
        )

    async def batch_remove_tags(self, sticker_ids: list[str], tags: list[str]) -> int:
        """批量移除标签"""
        if not sticker_ids or not tags:
            return 0

        remove = set(tags)

        def transform(current: list[str]) -> list[str]:
            return [t for t in current if t not in remove]

        placeholders = ",".join(["?"] * len(sticker_ids))
        return await self._rewrite_tags(
            transform, f"sticker_id IN ({placeholders})", tuple(sticker_ids)
        )

    async def batch_delete_stickers(self, sticker_ids: list[str]) -> int:
        """批量删除表情包记录（不含 stickers_bot 引用与磁盘文件清理）"""
        if not sticker_ids:
            return 0
        placeholders = ",".join(["?"] * len(sticker_ids))
        cursor = await self.conn.execute(
            f"DELETE FROM stickers WHERE sticker_id IN ({placeholders})",
            sticker_ids,
        )
        await self.conn.commit()
        return cursor.rowcount
