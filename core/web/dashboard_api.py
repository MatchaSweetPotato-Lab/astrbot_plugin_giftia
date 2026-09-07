import json
from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


class DashboardApi:
    """Dashboard UI preferences and configuration APIs."""

    def __init__(self, giftia=None):
        if giftia is not None:
            self.giftia = giftia

    async def get_nav_config(self):
        """Get saved dashboard navigation bar tabs configuration."""
        try:
            raw = await self.giftia.db.get_kv_data("dashboard_nav_config")
            if not raw:
                return json_response({"status": "success", "data": None})
            data = json.loads(raw) if isinstance(raw, str) else raw
            return json_response({"status": "success", "data": data})
        except Exception as e:
            logger.error(f"[Giftia API] get_nav_config error: {e}")
            return error_response("获取导航配置失败")

    async def save_nav_config(self):
        """Save dashboard navigation bar tabs configuration."""
        try:
            body = await request.json()
            if not isinstance(body, dict) or "config" not in body:
                return error_response("请求内容格式错误")
            config = body.get("config")
            if not isinstance(config, list):
                return error_response("导航配置必须是列表")

            cleaned_config = []
            for item in config:
                if isinstance(item, dict) and "id" in item:
                    cleaned_config.append({
                        "id": str(item["id"]),
                        "pinned": bool(item.get("pinned", False)),
                    })

            await self.giftia.db.upsert_kv_data(
                "dashboard_nav_config",
                json.dumps(cleaned_config, ensure_ascii=False),
            )
            return json_response({"status": "success", "data": cleaned_config})
        except Exception as e:
            logger.error(f"[Giftia API] save_nav_config error: {e}")
            return error_response("保存导航配置失败")
