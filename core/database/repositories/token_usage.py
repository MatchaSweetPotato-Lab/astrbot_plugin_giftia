import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from astrbot.api import logger

from .base import BaseRepository


class TokenUsageRepository(BaseRepository):
    async def log_token_usage(
        self,
        bot_name: str,
        group_or_user_id: str,
        type: str,
        provider_id: str,
        model_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        extra_info: dict | None = None,
    ):
        try:
            extra_info_str = (
                json.dumps(extra_info, ensure_ascii=False) if extra_info else None
            )
            await self.conn.execute(
                """
                INSERT INTO token_usage (
                    bot_name, group_or_user_id, type, provider_id, model_name,
                    prompt_tokens, completion_tokens, total_tokens, extra_info
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bot_name or "",
                    group_or_user_id or "",
                    type,
                    provider_id or "",
                    model_name or "",
                    prompt_tokens or 0,
                    completion_tokens or 0,
                    total_tokens or 0,
                    extra_info_str,
                ),
            )
            await self.conn.commit()
        except Exception as e:
            logger.error(f"[Database] Failed to log token usage: {e}")

    def _parse_db_utc_to_local_dt(self, utc_str: str) -> datetime:
        """解析数据库中的 UTC 时间字符串为带本地时区的 datetime 对象（支持带与不带小数秒）"""
        if not utc_str:
            return datetime.now().astimezone()
        try:
            return (
                datetime.fromisoformat(utc_str)
                .replace(tzinfo=timezone.utc)
                .astimezone()
            )
        except Exception as e:
            logger.error(f"[Database] Error parsing UTC string {utc_str}: {e}")
            return datetime.now().astimezone()

    def _local_str_to_utc_str(self, local_str: str) -> str:
        """本地时间字符串转为 UTC 时间字符串"""
        if not local_str:
            return local_str
        try:
            dt = datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S")
            utc_dt = dt.astimezone().astimezone(timezone.utc)
            return utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            logger.error(f"[Database] Error converting local to UTC: {e}")
            return local_str

    def _utc_str_to_local_str(self, utc_str: str) -> str:
        """数据库中的 UTC 时间字符串转为本地时间字符串"""
        if not utc_str:
            return utc_str
        try:
            local_dt = self._parse_db_utc_to_local_dt(utc_str)
            return local_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return utc_str

    def _get_time_range_thresholds(
        self, time_range: str
    ) -> tuple[str | None, str | None]:
        """返回对应的时间阈值 (usage_threshold, daily_threshold)"""
        if not time_range:
            return None, None
        now = datetime.now()
        if time_range == "today":
            today_str = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            return today_str, tomorrow_str
        elif time_range == "week":
            week_dt = now - timedelta(days=7)
            return week_dt.strftime("%Y-%m-%d %H:%M:%S"), week_dt.strftime("%Y-%m-%d")
        elif time_range == "month":
            month_dt = now - timedelta(days=30)
            return month_dt.strftime("%Y-%m-%d %H:%M:%S"), month_dt.strftime("%Y-%m-%d")
        elif time_range == "3months":
            m3_dt = now - timedelta(days=90)
            return m3_dt.strftime("%Y-%m-%d %H:%M:%S"), m3_dt.strftime("%Y-%m-%d")
        elif time_range == "year":
            year_dt = now - timedelta(days=365)
            return year_dt.strftime("%Y-%m-%d %H:%M:%S"), year_dt.strftime("%Y-%m-%d")
        return None, None

    async def get_token_stats(
        self,
        bot_name: str = None,
        group_or_user_id: str = None,
        time_range: str = None,
    ) -> dict:
        try:
            conditions_usage = []
            params_usage = []
            conditions_daily = []
            params_daily = []

            if bot_name:
                conditions_usage.append("bot_name = ?")
                params_usage.append(bot_name)
                conditions_daily.append("bot_name = ?")
                params_daily.append(bot_name)

            if group_or_user_id:
                conditions_usage.append("group_or_user_id = ?")
                params_usage.append(group_or_user_id)
                conditions_daily.append("group_or_user_id = ?")
                params_daily.append(group_or_user_id)

            if time_range:
                usage_thresh, daily_thresh = self._get_time_range_thresholds(time_range)
                if usage_thresh:
                    usage_thresh_utc = self._local_str_to_utc_str(usage_thresh)
                    conditions_usage.append("created_at >= ?")
                    params_usage.append(usage_thresh_utc)
                if daily_thresh:
                    conditions_daily.append("date >= ?")
                    params_daily.append(daily_thresh)

            where_usage = (
                f"WHERE {' AND '.join(conditions_usage)}" if conditions_usage else ""
            )
            where_daily = (
                f"WHERE {' AND '.join(conditions_daily)}" if conditions_daily else ""
            )

            # Query token_usage (今日明细). Keep provider_id in the grouping so
            # identically named models from different providers are not merged.
            sql_usage = f"""
                SELECT group_or_user_id, provider_id, model_name, type,
                       SUM(prompt_tokens) as prompt,
                       SUM(completion_tokens) as completion,
                       SUM(total_tokens) as total
                FROM token_usage
                {where_usage}
                GROUP BY group_or_user_id, provider_id, model_name, type
            """

            # Query token_daily_stats (历史归档)
            sql_daily = f"""
                SELECT group_or_user_id, provider_id, model_name, type,
                       SUM(prompt_tokens) as prompt,
                       SUM(completion_tokens) as completion,
                       SUM(total_tokens) as total
                FROM token_daily_stats
                {where_daily}
                GROUP BY group_or_user_id, provider_id, model_name, type
            """

            merged = defaultdict(lambda: {"prompt": 0, "completion": 0, "total": 0})

            async with self.conn.execute(sql_usage, params_usage) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    key = (
                        r["group_or_user_id"] or "",
                        r["provider_id"] or "",
                        r["model_name"] or "",
                        r["type"],
                    )
                    merged[key]["prompt"] += r["prompt"] or 0
                    merged[key]["completion"] += r["completion"] or 0
                    merged[key]["total"] += r["total"] or 0

            async with self.conn.execute(sql_daily, params_daily) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    key = (
                        r["group_or_user_id"] or "",
                        r["provider_id"] or "",
                        r["model_name"] or "",
                        r["type"],
                    )
                    merged[key]["prompt"] += r["prompt"] or 0
                    merged[key]["completion"] += r["completion"] or 0
                    merged[key]["total"] += r["total"] or 0

            summary = {
                "total_tokens": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_chars_tts": 0,
                "decision_tokens": 0,
                "decision_prompt_tokens": 0,
                "decision_completion_tokens": 0,
                "reply_tokens": 0,
                "reply_prompt_tokens": 0,
                "reply_completion_tokens": 0,
                "image_caption_tokens": 0,
                "audio_caption_tokens": 0,
                "tts_chars": 0,
                "passive_summary_tokens": 0,
                "sticker_analysis_tokens": 0,
            }

            group_map = defaultdict(lambda: {"tokens": 0, "tts_chars": 0})
            model_map = defaultdict(
                lambda: {"prompt": 0, "completion": 0, "total": 0, "tts_chars": 0}
            )

            for key, val in merged.items():
                group_id, provider_id, model_name, type_name = key
                prompt = val["prompt"]
                completion = val["completion"]
                total = val["total"]

                norm_provider_id = provider_id
                norm_model_name = model_name
                if provider_id and "/" in provider_id:
                    parts = provider_id.split("/")
                    norm_provider_id = parts[0]
                    norm_model_name = "/".join(parts[1:])

                if type_name != "tts":
                    summary["total_tokens"] += total
                    summary["total_prompt_tokens"] += prompt
                    summary["total_completion_tokens"] += completion

                    group_map[group_id]["tokens"] += total
                    model_map[(norm_provider_id, norm_model_name)]["prompt"] += prompt
                    model_map[(norm_provider_id, norm_model_name)]["completion"] += (
                        completion
                    )
                    model_map[(norm_provider_id, norm_model_name)]["total"] += total
                else:
                    summary["total_chars_tts"] += total
                    summary["tts_chars"] += total

                    group_map[group_id]["tts_chars"] += total
                    model_map[(norm_provider_id, norm_model_name)]["tts_chars"] += total

                if type_name == "decision":
                    summary["decision_tokens"] += total
                    summary["decision_prompt_tokens"] += prompt
                    summary["decision_completion_tokens"] += completion
                elif type_name == "reply":
                    summary["reply_tokens"] += total
                    summary["reply_prompt_tokens"] += prompt
                    summary["reply_completion_tokens"] += completion
                elif type_name == "image_caption":
                    summary["image_caption_tokens"] += total
                elif type_name == "audio_caption":
                    summary["audio_caption_tokens"] += total
                elif type_name == "passive_summary":
                    summary["passive_summary_tokens"] += total
                elif type_name == "sticker_analysis":
                    summary["sticker_analysis_tokens"] += total

            by_group = []
            for gid, gval in group_map.items():
                by_group.append(
                    {
                        "group_or_user_id": gid or "私聊/系统",
                        "total_tokens": gval["tokens"],
                        "total_chars_tts": gval["tts_chars"],
                    }
                )
            by_group.sort(key=lambda x: x["total_tokens"], reverse=True)

            by_model = []
            for (provider_id, model_name), mval in model_map.items():
                if mval["total"] > 0:
                    by_model.append(
                        {
                            "provider_id": provider_id or None,
                            "model_name": model_name or "未知",
                            "prompt_tokens": mval["prompt"],
                            "completion_tokens": mval["completion"],
                            "total_tokens": mval["total"],
                            "total_chars_tts": mval["tts_chars"],
                        }
                    )
            by_model.sort(key=lambda x: x["total_tokens"], reverse=True)

            # Calculate time series data
            unit = "day"
            min_date_str = None
            max_date_str = None
            if time_range == "today":
                unit = "hour"
            elif not time_range:  # 全部时间
                # Find min and max dates
                async with self.conn.execute(
                    "SELECT MIN(created_at), MAX(created_at) FROM token_usage"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0]:
                        min_date_str = self._utc_str_to_local_str(row[0])
                        max_date_str = self._utc_str_to_local_str(row[1])

                async with self.conn.execute(
                    "SELECT MIN(date), MAX(date) FROM token_daily_stats"
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0]:
                        if not min_date_str or row[0] < min_date_str.split(" ")[0]:
                            min_date_str = row[0] + " 00:00:00"
                        if not max_date_str or row[1] > max_date_str.split(" ")[0]:
                            max_date_str = row[1] + " 23:59:59"

                if min_date_str and max_date_str:
                    try:
                        min_dt = datetime.strptime(
                            min_date_str.split(".")[0], "%Y-%m-%d %H:%M:%S"
                        )
                        max_dt = datetime.strptime(
                            max_date_str.split(".")[0], "%Y-%m-%d %H:%M:%S"
                        )
                        if (max_dt - min_dt).days > 30:
                            unit = "month"
                    except Exception:
                        pass

            # Pre-populate time buckets
            now = datetime.now()
            time_buckets = []
            if unit == "hour":
                time_buckets = [f"{h:02d}" for h in range(24)]
            elif unit == "day":
                days_count = 30
                if time_range == "week":
                    days_count = 7
                elif time_range == "month":
                    days_count = 30
                elif time_range == "3months":
                    days_count = 90
                elif time_range == "year":
                    days_count = 365
                elif not time_range and min_date_str:
                    try:
                        min_dt = datetime.strptime(
                            min_date_str.split(" ")[0], "%Y-%m-%d"
                        )
                        days_count = (now - min_dt).days + 1
                    except Exception:
                        pass
                days_count = max(1, min(days_count, 365))
                time_buckets = [
                    (now - timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range(days_count)
                ]
                time_buckets.reverse()
            elif unit == "month":
                start_year = now.year
                start_month = now.month
                if min_date_str:
                    try:
                        start_year = int(min_date_str[:4])
                        start_month = int(min_date_str[5:7])
                    except Exception:
                        pass

                curr_y, curr_m = start_year, start_month
                while (curr_y < now.year) or (
                    curr_y == now.year and curr_m <= now.month
                ):
                    time_buckets.append(f"{curr_y}-{curr_m:02d}")
                    curr_m += 1
                    if curr_m > 12:
                        curr_m = 1
                        curr_y += 1

            raw_points = []
            # 1. From token_usage
            sql_ts_usage = f"""
                SELECT strftime('%Y-%m-%d %H:%M:%S', datetime(created_at, 'localtime')) as local_time,
                       provider_id, model_name, type, group_or_user_id, total_tokens
                FROM token_usage
                {where_usage}
            """
            async with self.conn.execute(sql_ts_usage, params_usage) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    raw_points.append(
                        {
                            "local_time": r["local_time"],
                            "provider_id": r["provider_id"] or "",
                            "model_name": r["model_name"] or "",
                            "type": r["type"] or "",
                            "group_or_user_id": r["group_or_user_id"] or "",
                            "total": r["total_tokens"] or 0,
                        }
                    )

            # 2. From token_daily_stats
            if unit != "hour":
                sql_ts_daily = f"""
                    SELECT date, provider_id, model_name, type, group_or_user_id, total_tokens
                    FROM token_daily_stats
                    {where_daily}
                """
                async with self.conn.execute(sql_ts_daily, params_daily) as cursor:
                    rows = await cursor.fetchall()
                    for r in rows:
                        raw_points.append(
                            {
                                "local_time": r["date"] + " 00:00:00",
                                "provider_id": r["provider_id"] or "",
                                "model_name": r["model_name"] or "",
                                "type": r["type"] or "",
                                "group_or_user_id": r["group_or_user_id"] or "",
                                "total": r["total_tokens"] or 0,
                            }
                        )

            model_totals = defaultdict(int)
            type_totals = defaultdict(int)
            group_totals = defaultdict(int)

            for p in raw_points:
                provider_id = p["provider_id"]
                model_name = p["model_name"]
                norm_provider_id = provider_id
                norm_model_name = model_name
                if provider_id and "/" in provider_id:
                    parts = provider_id.split("/")
                    norm_provider_id = parts[0]
                    norm_model_name = "/".join(parts[1:])

                m_key = (
                    f"{norm_provider_id}/{norm_model_name}"
                    if norm_provider_id
                    else norm_model_name
                )
                t_key = p["type"]
                g_key = p["group_or_user_id"] or "私聊/系统"
                if t_key != "tts":
                    model_totals[m_key] += p["total"]
                    type_totals[t_key] += p["total"]
                    group_totals[g_key] += p["total"]

            top_models = set(
                sorted(
                    model_totals.keys(), key=lambda x: model_totals[x], reverse=True
                )[:5]
            )
            top_types = set(
                sorted(type_totals.keys(), key=lambda x: type_totals[x], reverse=True)[
                    :5
                ]
            )
            top_groups = set(
                sorted(
                    group_totals.keys(), key=lambda x: group_totals[x], reverse=True
                )[:5]
            )

            timeline_data = {
                b: {
                    "model": defaultdict(int),
                    "type": defaultdict(int),
                    "group": defaultdict(int),
                }
                for b in time_buckets
            }

            for p in raw_points:
                t_str = p["local_time"]
                if unit == "hour":
                    bucket_key = t_str[11:13]
                elif unit == "day":
                    bucket_key = t_str[:10]
                else:
                    bucket_key = t_str[:7]

                if bucket_key not in timeline_data:
                    continue

                provider_id = p["provider_id"]
                model_name = p["model_name"]
                norm_provider_id = provider_id
                norm_model_name = model_name
                if provider_id and "/" in provider_id:
                    parts = provider_id.split("/")
                    norm_provider_id = parts[0]
                    norm_model_name = "/".join(parts[1:])

                m_key = (
                    f"{norm_provider_id}/{norm_model_name}"
                    if norm_provider_id
                    else norm_model_name
                )
                t_key = p["type"]
                g_key = p["group_or_user_id"] or "私聊/系统"
                tokens = p["total"]

                if t_key != "tts":
                    if m_key in top_models:
                        timeline_data[bucket_key]["model"][m_key] += tokens
                    else:
                        timeline_data[bucket_key]["model"]["其他"] += tokens

                    if t_key in top_types:
                        timeline_data[bucket_key]["type"][t_key] += tokens
                    else:
                        timeline_data[bucket_key]["type"]["其他"] += tokens

                    if g_key in top_groups:
                        timeline_data[bucket_key]["group"][g_key] += tokens
                    else:
                        timeline_data[bucket_key]["group"]["其他"] += tokens

            timeline_list = []
            for b in time_buckets:
                timeline_list.append(
                    {
                        "time": b,
                        "model": dict(timeline_data[b]["model"]),
                        "type": dict(timeline_data[b]["type"]),
                        "group": dict(timeline_data[b]["group"]),
                    }
                )

            time_series = {"unit": unit, "timeline": timeline_list}

            return {
                "summary": summary,
                "by_group": by_group,
                "by_model": by_model,
                "time_series": time_series,
            }
        except Exception as e:
            logger.error(f"[Database] get_token_stats error: {e}", exc_info=True)
            return {
                "summary": {},
                "by_group": [],
                "by_model": [],
                "time_series": {"unit": "day", "timeline": []},
            }

    async def get_token_logs(
        self,
        page: int = 1,
        page_size: int = 20,
        type: str = None,
        bot_name: str = None,
        group_or_user_id: str = None,
        time_range: str = None,
    ) -> tuple[list[dict], int]:
        try:
            conditions = []
            params = []
            if type:
                conditions.append("type = ?")
                params.append(type)
            if bot_name:
                conditions.append("bot_name = ?")
                params.append(bot_name)
            if group_or_user_id:
                conditions.append("group_or_user_id = ?")
                params.append(group_or_user_id)
            if time_range:
                now = datetime.now()
                if time_range == "today":
                    today_str = now.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    today_str_utc = self._local_str_to_utc_str(today_str)
                    conditions.append("created_at >= ?")
                    params.append(today_str_utc)
                elif time_range == "week":
                    week_str = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                    week_str_utc = self._local_str_to_utc_str(week_str)
                    conditions.append("created_at >= ?")
                    params.append(week_str_utc)
                elif time_range == "month":
                    month_str = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
                    month_str_utc = self._local_str_to_utc_str(month_str)
                    conditions.append("created_at >= ?")
                    params.append(month_str_utc)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            # Count total
            sql_count = f"SELECT COUNT(*) as count FROM token_usage {where_clause}"
            async with self.conn.execute(sql_count, params) as cursor:
                total_count = (await cursor.fetchone())["count"]

            # Query limit offset
            offset = (page - 1) * page_size
            sql_select = f"""
                SELECT id, bot_name, group_or_user_id, type, provider_id, model_name,
                       prompt_tokens, completion_tokens, total_tokens, created_at
                FROM token_usage
                {where_clause}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            """
            select_params = params + [page_size, offset]
            logs = []
            async with self.conn.execute(sql_select, select_params) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    created_at_str = self._utc_str_to_local_str(r["created_at"])
                    logs.append(
                        {
                            "id": r["id"],
                            "bot_name": r["bot_name"],
                            "group_or_user_id": r["group_or_user_id"],
                            "type": r["type"],
                            "provider_id": r["provider_id"],
                            "model_name": r["model_name"],
                            "prompt_tokens": r["prompt_tokens"],
                            "completion_tokens": r["completion_tokens"],
                            "total_tokens": r["total_tokens"],
                            "created_at": created_at_str,
                        }
                    )

            return logs, total_count
        except Exception as e:
            logger.error(f"[Database] get_token_logs error: {e}")
            return [], 0

    async def squash_token_logs(self) -> int:
        """将昨日及以前的详细 Token 日志合并压扁并归档，随后清理详细明细"""
        try:
            # 1. 确定今日凌晨的临界时间点（本地时间）
            cutoff = (
                datetime.now()
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .strftime("%Y-%m-%d %H:%M:%S")
            )
            cutoff_utc = self._local_str_to_utc_str(cutoff)

            # 2. 查询所有早于临界点的详细记录
            sql_select = """
                SELECT bot_name, group_or_user_id, type, provider_id, model_name,
                       prompt_tokens, completion_tokens, total_tokens, extra_info, created_at, call_count
                FROM token_usage
                WHERE created_at < ?
            """

            rows_to_squash = []
            async with self.conn.execute(sql_select, (cutoff_utc,)) as cursor:
                rows = await cursor.fetchall()
                for r in rows:
                    rows_to_squash.append(
                        {
                            "bot_name": r["bot_name"],
                            "group_or_user_id": r["group_or_user_id"],
                            "type": r["type"],
                            "provider_id": r["provider_id"],
                            "model_name": r["model_name"],
                            "prompt_tokens": r["prompt_tokens"] or 0,
                            "completion_tokens": r["completion_tokens"] or 0,
                            "total_tokens": r["total_tokens"] or 0,
                            "extra_info": r["extra_info"],
                            "created_at": r["created_at"],
                            "call_count": dict(r).get("call_count", 1) or 1,
                        }
                    )

            if not rows_to_squash:
                return 0

            # 3. 按维度分类累加
            daily_sums = defaultdict(
                lambda: {"prompt": 0, "completion": 0, "total": 0, "count": 0}
            )
            for r in rows_to_squash:
                # 提取日期 YYYY-MM-DD (按本地时间提取)
                date_str = None
                if r["created_at"]:
                    try:
                        local_dt = self._parse_db_utc_to_local_dt(r["created_at"])
                        date_str = local_dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass
                if not date_str:
                    date_str = datetime.now().strftime("%Y-%m-%d")

                # 提取 status 字段
                status = "success"
                if r["extra_info"]:
                    try:
                        info = json.loads(r["extra_info"])
                        status = info.get("status", "success")
                    except Exception:
                        pass

                key = (
                    date_str,
                    r["bot_name"] or "",
                    r["group_or_user_id"] or "",
                    r["type"],
                    r["provider_id"] or "",
                    r["model_name"] or "",
                    status,
                )
                daily_sums[key]["prompt"] += r["prompt_tokens"]
                daily_sums[key]["completion"] += r["completion_tokens"]
                daily_sums[key]["total"] += r["total_tokens"]
                daily_sums[key]["count"] += r["call_count"]

            # 4. 批量 Upsert 合并到历史表
            upsert_count = 0
            for key, sums in daily_sums.items():
                (
                    date_val,
                    bot_val,
                    group_val,
                    type_val,
                    provider_val,
                    model_val,
                    status_val,
                ) = key
                await self.conn.execute(
                    """
                    INSERT INTO token_daily_stats (
                        date, bot_name, group_or_user_id, type, provider_id, model_name, status,
                        prompt_tokens, completion_tokens, total_tokens, call_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, bot_name, group_or_user_id, type, provider_id, model_name, status)
                    DO UPDATE SET
                        prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                        completion_tokens = completion_tokens + excluded.completion_tokens,
                        total_tokens = total_tokens + excluded.total_tokens,
                        call_count = call_count + excluded.call_count
                    """,
                    (
                        date_val,
                        bot_val,
                        group_val,
                        type_val,
                        provider_val,
                        model_val,
                        status_val,
                        sums["prompt"],
                        sums["completion"],
                        sums["total"],
                        sums["count"],
                    ),
                )
                upsert_count += 1

            # 5. 删除已拍扁的详细日志
            cursor_del = await self.conn.execute(
                "DELETE FROM token_usage WHERE created_at < ?", (cutoff_utc,)
            )
            deleted_count = cursor_del.rowcount
            await self.conn.commit()

            logger.info(
                f"[Database] Squash completed: consolidated {deleted_count} detailed rows into {upsert_count} summary rows."
            )
            return deleted_count
        except Exception as e:
            logger.error(f"[Database] squash_token_logs error: {e}", exc_info=True)
            return 0

    async def clear_token_logs(
        self,
        before_days: int = None,
        bot_name: str = None,
        group_or_user_id: str = None,
        time_range: str = None,
    ) -> int:
        try:
            # 1. Clear token_usage matching filters
            conds_usage = []
            params_usage = []
            if before_days is not None:
                cutoff = (datetime.now() - timedelta(days=before_days)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                cutoff_utc = self._local_str_to_utc_str(cutoff)
                conds_usage.append("created_at < ?")
                params_usage.append(cutoff_utc)
            if bot_name:
                conds_usage.append("bot_name = ?")
                params_usage.append(bot_name)
            if group_or_user_id:
                conds_usage.append("group_or_user_id = ?")
                params_usage.append(group_or_user_id)
            if time_range:
                usage_thresh, _ = self._get_time_range_thresholds(time_range)
                if usage_thresh:
                    usage_thresh_utc = self._local_str_to_utc_str(usage_thresh)
                    conds_usage.append("created_at >= ?")
                    params_usage.append(usage_thresh_utc)

            where_usage = f"WHERE {' AND '.join(conds_usage)}" if conds_usage else ""
            sql_usage = f"DELETE FROM token_usage {where_usage}"
            cursor_usage = await self.conn.execute(sql_usage, params_usage)
            usage_deleted = cursor_usage.rowcount

            # 2. Clear token_daily_stats matching filters
            conds_daily = []
            params_daily = []
            if before_days is not None:
                cutoff_date = (datetime.now() - timedelta(days=before_days)).strftime(
                    "%Y-%m-%d"
                )
                conds_daily.append("date < ?")
                params_daily.append(cutoff_date)
            if bot_name:
                conds_daily.append("bot_name = ?")
                params_daily.append(bot_name)
            if group_or_user_id:
                conds_daily.append("group_or_user_id = ?")
                params_daily.append(group_or_user_id)
            if time_range:
                _, daily_thresh = self._get_time_range_thresholds(time_range)
                if daily_thresh:
                    conds_daily.append("date >= ?")
                    params_daily.append(daily_thresh)

            where_daily = f"WHERE {' AND '.join(conds_daily)}" if conds_daily else ""
            sql_daily = f"DELETE FROM token_daily_stats {where_daily}"
            cursor_daily = await self.conn.execute(sql_daily, params_daily)
            daily_deleted = cursor_daily.rowcount

            await self.conn.commit()
            return usage_deleted + daily_deleted
        except Exception as e:
            logger.error(f"[Database] clear_token_logs error: {e}", exc_info=True)
            return 0
