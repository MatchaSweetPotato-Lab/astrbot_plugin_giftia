import json
from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


class TokenApi:
    """Token statistics and logs management APIs."""

    def __init__(self, giftia):
        self.giftia = giftia

    async def get_token_stats(self) -> dict:
        """获取 Token 消耗统计概览与图表数据"""
        try:
            bot_name = request.query.get("bot_name")
            group_or_user_id = request.query.get("group_or_user_id")
            time_range = request.query.get("time_range")

            stats = await self.giftia.db.get_token_stats(
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                time_range=time_range,
            )
            return json_response({"status": "success", "stats": stats})
        except Exception as e:
            logger.error(f"[Giftia API] get_token_stats error: {e}")
            return error_response(f"获取 Token 统计数据失败: {str(e)}")

    async def clear_token_logs(self) -> dict:
        """根据当前筛选条件清空 Token 消耗日志"""
        try:
            body = None
            try:
                body = await request.json()
            except Exception:
                pass
            
            before_days = body.get("before_days") if body else None
            if before_days is not None:
                before_days = int(before_days)
            
            bot_name = body.get("bot_name") if body else None
            group_or_user_id = body.get("group_or_user_id") if body else None
            time_range = body.get("time_range") if body else None

            cleaned_count = await self.giftia.db.clear_token_logs(
                before_days=before_days,
                bot_name=bot_name,
                group_or_user_id=group_or_user_id,
                time_range=time_range,
            )
            return json_response({
                "status": "success",
                "message": f"成功清除了 {cleaned_count} 条 Token 消耗日志记录",
            })
        except Exception as e:
            logger.error(f"[Giftia API] clear_token_logs error: {e}")
            return error_response(f"清除 Token 消耗日志失败: {str(e)}")

    async def get_auto_clean_token_config(self) -> dict:
        """获取自动清理 Token 配置"""
        try:
            raw_cfg = await self.giftia.db.get_kv_data("auto_clean_token_config")
            cfg = (
                json.loads(raw_cfg)
                if raw_cfg
                else {"enabled": True, "days": 365}
            )
            return json_response({"status": "success", "config": cfg})
        except Exception as e:
            logger.error(f"[Giftia API] get_auto_clean_token_config error: {e}")
            return error_response(f"获取自动清理配置失败: {str(e)}")

    async def set_auto_clean_token_config(self) -> dict:
        """设置自动清理 Token 配置"""
        try:
            body = await request.json()
            enabled = bool(body.get("enabled", True))
            days = int(body.get("days", 365))

            cfg = {"enabled": enabled, "days": days}
            await self.giftia.db.upsert_kv_data(
                "auto_clean_token_config", json.dumps(cfg)
            )

            # 更新调度器任务
            self.giftia.tools_func.update_auto_clean_token_job()

            return json_response({"status": "success", "message": "自动清理配置更新成功"})
        except Exception as e:
            logger.error(f"[Giftia API] set_auto_clean_token_config error: {e}")
            return error_response(f"更新自动清理配置失败: {str(e)}")

    async def trigger_auto_clean_token(self) -> dict:
        """手动触发 Token 日志自动清理"""
        try:
            res = await self.giftia.tools_func.auto_clean_token_usage()
            if isinstance(res, dict) and res.get("status") == "error":
                return error_response(res.get("message", "触发清理失败"))
            return json_response(res)
        except Exception as e:
            logger.error(f"[Giftia API] trigger_auto_clean_token error: {e}")
            return error_response(f"手动触发 Token 清理失败: {str(e)}")
