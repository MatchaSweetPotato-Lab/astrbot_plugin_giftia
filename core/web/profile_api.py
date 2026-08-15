import json

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from .web_helpers import USER_PROFILE_FIELD_KEYS


class ProfileApi:
    """User profile, user alias, and group profile Web APIs."""

    def _invalidate_user_profile_record_cache(
        self, bot_name: str, group_or_user_id: str, user_id: str
    ) -> None:
        fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
        self.giftia.data_cache.user_profile_records.pop(fmt_key, None)

    # ── User Profile APIs ───────────────────────────────────────────────

    async def get_user_profiles(self):
        """Get user profiles with pagination and filters."""
        try:
            page = int(request.query.get("page", 1))
            limit = int(request.query.get("limit", 20))
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            user_id = request.query.get("user_id")
            search = request.query.get("search")

            offset = (page - 1) * limit
            conditions = []
            params = []

            if bot_name:
                conditions.append("up.bot_name = ?")
                params.append(bot_name)
            if group_or_user_id:
                conditions.append("up.group_or_user_id = ?")
                params.append(group_or_user_id)
            if user_id:
                conditions.append("up.user_id = ?")
                params.append(user_id)
            if search:
                like_fields = [
                    f"up.{field}" for field in USER_PROFILE_FIELD_KEYS
                ]
                alias_exists = """
                    EXISTS (
                        SELECT 1
                        FROM user_aliases ua
                        WHERE ua.bot_name = up.bot_name
                          AND ua.group_or_user_id = up.group_or_user_id
                          AND ua.user_id = up.user_id
                          AND ua.alias LIKE ?
                    )
                """
                conditions.append(
                    "("
                    + " OR ".join(
                        [f"{field} LIKE ?" for field in like_fields] + [alias_exists]
                    )
                    + ")"
                )
                params.extend([f"%{search}%"] * (len(like_fields) + 1))

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            # Query count
            count_sql = f"SELECT COUNT(*) as total FROM user_profiles up {where_clause}"
            async with self.giftia.db.conn.execute(count_sql, params) as cursor:
                row = await cursor.fetchone()
                total = row["total"] if row else 0

            # Query data
            data_sql = f"""
                SELECT up.id, up.bot_name, up.group_or_user_id, up.user_id,
                       up.call_name, up.personality,
                       up.interests, up.attitude, up.agreements, up.extra,
                       up.created_at, up.updated_at,
                       COALESCE(up.relation, r.relation) AS relation,
                       CASE WHEN up.title IS NOT NULL THEN up.title ELSE r.title END AS title
                FROM user_profiles up
                LEFT JOIN relations r ON up.bot_name = r.bot_name AND up.group_or_user_id = r.group_or_user_id AND up.user_id = r.user_id
                {where_clause}
                ORDER BY up.updated_at DESC
                LIMIT ? OFFSET ?
            """
            data_params = params + [limit, offset]
            items = []
            async with self.giftia.db.conn.execute(data_sql, data_params) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    items.append(
                        {
                            "id": r["id"],
                            "bot_name": r["bot_name"],
                            "group_or_user_id": r["group_or_user_id"],
                            "user_id": r["user_id"],
                            "call_name": r["call_name"] or "",
                            "aliases": await self.giftia.db.get_user_aliases_text(
                                bot_name=r["bot_name"],
                                group_or_user_id=r["group_or_user_id"],
                                user_id=r["user_id"],
                                limit=6,
                            ),
                            "personality": r["personality"] or "",
                            "interests": r["interests"] or "",
                            "attitude": r["attitude"] or "",
                            "agreements": r["agreements"] or "",
                            "extra": r["extra"] or "",
                            "relation": r["relation"]
                            if r["relation"] is not None
                            else 0,
                            "title": r["title"] if r["title"] is not None else "",
                            "created_at": r["created_at"],
                            "updated_at": r["updated_at"],
                        }
                    )

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": items,
                        "total": total,
                        "page": page,
                        "limit": limit,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_user_profiles error: {e}")
            return error_response(f"获取用户画像列表失败: {str(e)}")

    async def get_user_profile_filter_options(self):
        """Get bot/session filter options for user profiles."""
        try:
            bot_name = request.query.get("bot_name")
            user_id = request.query.get("user_id")
            search = request.query.get("search")

            bots = []
            async with self.giftia.db.conn.execute(
                """
                SELECT DISTINCT bot_name
                FROM user_profiles
                WHERE bot_name IS NOT NULL AND bot_name != ''
                ORDER BY bot_name ASC
                """
            ) as cursor:
                rows = await cursor.fetchall()
                bots = [row["bot_name"] for row in rows if row["bot_name"]]

            selected_bot_name = (
                bot_name if bot_name in bots else (bots[0] if bots else "")
            )

            sessions = []
            if selected_bot_name:
                conditions = ["bot_name = ?"]
                params = [selected_bot_name]
                if user_id:
                    conditions.append("user_id = ?")
                    params.append(user_id)
                if search:
                    like_fields = ["profile"] + list(USER_PROFILE_FIELD_KEYS)
                    alias_exists = """
                        EXISTS (
                            SELECT 1
                            FROM user_aliases ua
                            WHERE ua.bot_name = user_profiles.bot_name
                              AND ua.group_or_user_id = user_profiles.group_or_user_id
                              AND ua.user_id = user_profiles.user_id
                              AND ua.alias LIKE ?
                        )
                    """
                    conditions.append(
                        "("
                        + " OR ".join(
                            [f"{field} LIKE ?" for field in like_fields]
                            + [alias_exists]
                        )
                        + ")"
                    )
                    params.extend([f"%{search}%"] * (len(like_fields) + 1))

                where_clause = "WHERE " + " AND ".join(conditions)
                async with self.giftia.db.conn.execute(
                    f"""
                    SELECT group_or_user_id, COUNT(*) as total, MAX(updated_at) as latest_at
                    FROM user_profiles
                    {where_clause}
                    GROUP BY group_or_user_id
                    ORDER BY latest_at DESC, group_or_user_id ASC
                    """,
                    params,
                ) as cursor:
                    rows = await cursor.fetchall()
                    sessions = [
                        {
                            "group_or_user_id": row["group_or_user_id"],
                            "total": row["total"],
                        }
                        for row in rows
                        if row["group_or_user_id"]
                    ]

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "bots": bots,
                        "selected_bot_name": selected_bot_name,
                        "sessions": sessions,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_user_profile_filter_options error: {e}")
            return error_response(f"获取用户画像筛选项失败: {str(e)}")

    async def update_user_profile(self):
        """Update/Upsert user profile."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")
            relation = body.get("relation")
            title = body.get("title")
            profile_fields = {
                field: body.get(field)
                for field in USER_PROFILE_FIELD_KEYS
                if field in body
            }

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )

            parsed_relation = (
                int(relation) if relation is not None and relation != "" else None
            )
            parsed_title = str(title) if title is not None else None

            await self.giftia.data_cache.set_user_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                relation=parsed_relation,
                title=parsed_title,
                profile_fields=profile_fields,
                alias_increment_count=False,
            )

            return json_response({"status": "success", "message": "更新用户画像成功"})
        except Exception as e:
            logger.error(f"[Giftia API] update_user_profile error: {e}")
            return error_response(f"更新用户画像失败: {str(e)}")

    async def delete_user_profile(self):
        """Delete user profile."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )

            await self.giftia.db.delete_user_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
            )
            fmt_key = f"{bot_name}:{group_or_user_id}:{user_id}"
            self.giftia.data_cache.user_profiles.pop(fmt_key, None)
            self.giftia.data_cache.user_profile_records.pop(fmt_key, None)
            self.giftia.data_cache.relations.pop(fmt_key, None)
            return json_response({"status": "success", "message": "删除用户画像成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_user_profile error: {e}")
            return error_response(f"删除用户画像失败: {str(e)}")

    # ── User Aliases APIs ────────────────────────────────────────────────

    async def get_user_aliases(self):
        """Get all aliases for a user profile."""
        try:
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            user_id = request.query.get("user_id")
            limit_raw = request.query.get("limit")

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )

            limit = int(limit_raw) if limit_raw else None
            aliases = await self.giftia.db.get_user_aliases(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                limit=limit,
                ignore_count_filter=True,
            )
            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": aliases,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_user_aliases error: {e}")
            return error_response(f"获取用户外号失败: {str(e)}")

    async def add_user_alias(self):
        """Add one or more aliases for a user without increasing existing counts."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")
            alias = str(body.get("alias") or "").strip()

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )
            if not alias:
                return error_response("外号不能为空")

            await self.giftia.db.upsert_user_aliases(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                aliases=alias,
                increment_count=False,
            )
            self._invalidate_user_profile_record_cache(
                bot_name, group_or_user_id, user_id
            )
            return json_response({"status": "success", "message": "新增外号成功"})
        except Exception as e:
            logger.error(f"[Giftia API] add_user_alias error: {e}")
            return error_response(f"新增用户外号失败: {str(e)}")

    async def update_user_alias_count(self):
        """Update alias count for a user."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")
            alias = str(body.get("alias") or "").strip()
            alias_count = body.get("alias_count")

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )
            if not alias:
                return error_response("外号不能为空")
            try:
                parsed_count = int(alias_count)
            except (TypeError, ValueError):
                return error_response("统计次数必须是正整数")
            if parsed_count < 1:
                return error_response("统计次数必须大于 0")

            updated = await self.giftia.db.set_user_alias_count(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                alias=alias,
                alias_count=parsed_count,
            )
            if not updated:
                return error_response("外号不存在")
            self._invalidate_user_profile_record_cache(
                bot_name, group_or_user_id, user_id
            )
            return json_response({"status": "success", "message": "更新外号次数成功"})
        except Exception as e:
            logger.error(f"[Giftia API] update_user_alias_count error: {e}")
            return error_response(f"更新用户外号次数失败: {str(e)}")

    async def delete_user_alias(self):
        """Delete one alias for a user."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            user_id = body.get("user_id")
            alias = str(body.get("alias") or "").strip()

            if not bot_name or not group_or_user_id or not user_id:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, user_id)"
                )
            if not alias:
                return error_response("外号不能为空")

            deleted = await self.giftia.db.delete_user_alias(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                user_id=user_id,
                alias=alias,
            )
            if not deleted:
                return error_response("外号不存在")
            self._invalidate_user_profile_record_cache(
                bot_name, group_or_user_id, user_id
            )
            return json_response({"status": "success", "message": "删除外号成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_user_alias error: {e}")
            return error_response(f"删除用户外号失败: {str(e)}")

    # ── User Aliases Auto Clean APIs ───────────────────────────────────

    async def get_auto_clean_aliases_config(self):
        """获取过期外号自动清理配置。"""
        try:
            raw_cfg = await self.giftia.db.get_kv_data("auto_clean_user_aliases_config")
            cfg = self.giftia.tools_func.normalize_auto_clean_aliases_config(raw_cfg)
            return json_response({"status": "success", "config": cfg})
        except Exception as e:
            logger.error(f"[Giftia API] get_auto_clean_aliases_config error: {e}")
            return error_response(f"获取过期外号自动清理配置失败: {str(e)}")

    async def set_auto_clean_aliases_config(self):
        """保存过期外号自动清理配置。"""
        try:
            body = await request.json()
            cfg = self.giftia.tools_func.normalize_auto_clean_aliases_config(body)
            await self.giftia.db.upsert_kv_data(
                "auto_clean_user_aliases_config", json.dumps(cfg)
            )
            return json_response({"status": "success", "config": cfg, "message": "已保存过期外号自动清理配置"})
        except Exception as e:
            logger.error(f"[Giftia API] set_auto_clean_aliases_config error: {e}")
            return error_response(f"保存过期外号自动清理配置失败: {str(e)}")

    async def trigger_auto_clean_aliases(self):
        """立即手动触发过期外号自动清理。"""
        try:
            res = await self.giftia.tools_func.auto_clean_expired_user_aliases()
            return json_response(res)
        except Exception as e:
            logger.error(f"[Giftia API] trigger_auto_clean_aliases error: {e}")
            return error_response(f"执行过期外号自动清理失败: {str(e)}")

    # ── Group Profile APIs ──────────────────────────────────────────────

    async def get_group_profiles(self):
        """Get group profiles with pagination and filters."""
        try:
            page = int(request.query.get("page", 1))
            limit = int(request.query.get("limit", 20))
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            search = request.query.get("search")

            offset = (page - 1) * limit
            conditions = []
            params = []

            if bot_name:
                conditions.append("bot_name = ?")
                params.append(bot_name)
            if group_or_user_id:
                conditions.append("group_or_user_id = ?")
                params.append(group_or_user_id)
            if search:
                conditions.append("profile LIKE ?")
                params.append(f"%{search}%")

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            # Query count
            count_sql = f"SELECT COUNT(*) as total FROM group_profiles {where_clause}"
            async with self.giftia.db.conn.execute(count_sql, params) as cursor:
                row = await cursor.fetchone()
                total = row["total"] if row else 0

            # Query data
            data_sql = f"""
                SELECT id, bot_name, group_or_user_id, profile, created_at, updated_at
                FROM group_profiles
                {where_clause}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
            """
            data_params = params + [limit, offset]
            items = []
            async with self.giftia.db.conn.execute(data_sql, data_params) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    items.append(
                        {
                            "id": r["id"],
                            "bot_name": r["bot_name"],
                            "group_or_user_id": r["group_or_user_id"],
                            "profile": r["profile"],
                            "created_at": r["created_at"],
                            "updated_at": r["updated_at"],
                        }
                    )

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "items": items,
                        "total": total,
                        "page": page,
                        "limit": limit,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_group_profiles error: {e}")
            return error_response(f"获取群聊画像列表失败: {str(e)}")

    async def get_group_profile_filter_options(self):
        """Get bot/session filter options for group profiles."""
        try:
            bot_name = request.query.get("bot_name")
            search = request.query.get("search")

            bots = []
            async with self.giftia.db.conn.execute(
                """
                SELECT DISTINCT bot_name
                FROM group_profiles
                WHERE bot_name IS NOT NULL AND bot_name != ''
                ORDER BY bot_name ASC
                """
            ) as cursor:
                rows = await cursor.fetchall()
                bots = [row["bot_name"] for row in rows if row["bot_name"]]

            selected_bot_name = (
                bot_name if bot_name in bots else (bots[0] if bots else "")
            )

            sessions = []
            if selected_bot_name:
                conditions = ["bot_name = ?"]
                params = [selected_bot_name]
                if search:
                    conditions.append("profile LIKE ?")
                    params.append(f"%{search}%")

                where_clause = "WHERE " + " AND ".join(conditions)
                async with self.giftia.db.conn.execute(
                    f"""
                    SELECT group_or_user_id, COUNT(*) as total, MAX(updated_at) as latest_at
                    FROM group_profiles
                    {where_clause}
                    GROUP BY group_or_user_id
                    ORDER BY latest_at DESC, group_or_user_id ASC
                    """,
                    params,
                ) as cursor:
                    rows = await cursor.fetchall()
                    sessions = [
                        {
                            "group_or_user_id": row["group_or_user_id"],
                            "total": row["total"],
                        }
                        for row in rows
                        if row["group_or_user_id"]
                    ]

            return json_response(
                {
                    "status": "success",
                    "data": {
                        "bots": bots,
                        "selected_bot_name": selected_bot_name,
                        "sessions": sessions,
                    },
                }
            )
        except Exception as e:
            logger.error(f"[Giftia API] get_group_profile_filter_options error: {e}")
            return error_response(f"获取群画像筛选项失败: {str(e)}")

    async def update_group_profile(self):
        """Update/Upsert group profile."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")
            profile = body.get("profile")

            if not bot_name or not group_or_user_id or profile is None:
                return error_response(
                    "缺少必要参数 (bot_name, group_or_user_id, profile)"
                )

            await self.giftia.db.upsert_group_profile(
                group_or_user_id=group_or_user_id,
                bot_name=bot_name,
                profile=profile,
            )
            return json_response({"status": "success", "message": "更新群聊画像成功"})
        except Exception as e:
            logger.error(f"[Giftia API] update_group_profile error: {e}")
            return error_response(f"更新群聊画像失败: {str(e)}")

    async def delete_group_profile(self):
        """Delete group profile."""
        try:
            body = await request.json()
            bot_name = body.get("bot_name")
            group_or_user_id = body.get("group_or_user_id")

            if not bot_name or not group_or_user_id:
                return error_response("缺少必要参数 (bot_name, group_or_user_id)")

            await self.giftia.db.delete_group_profile(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
            )
            return json_response({"status": "success", "message": "删除群聊画像成功"})
        except Exception as e:
            logger.error(f"[Giftia API] delete_group_profile error: {e}")
            return error_response(f"删除群聊画像失败: {str(e)}")
