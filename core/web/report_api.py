import asyncio
import base64
from pathlib import Path

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request


class ReportApi:
    async def get_report_templates(self):
        try:
            service = self.giftia.reports
            templates = await asyncio.to_thread(
                lambda: [service.get_template(key) for key in service.definitions]
            )
            return json_response({"status": "success", "data": templates})
        except Exception:
            logger.exception("[Giftia Reports] Failed to load templates")
            return error_response("读取报告模板失败", status_code=500)

    async def update_report_template(self):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求必须是 JSON 对象")
            report_type = body.get("report_type")
            if not isinstance(report_type, str):
                return error_response("缺少报告类型")
            await asyncio.to_thread(
                self.giftia.reports.save_template, report_type, body.get("html")
            )
            return json_response({"status": "success", "message": "模板已保存"})
        except ValueError as exc:
            return error_response(str(exc))
        except Exception:
            logger.exception("[Giftia Reports] Failed to save template")
            return error_response("保存模板失败", status_code=500)

    async def preview_report(self):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求必须是 JSON 对象")
            report_type = body.get("report_type")
            if not isinstance(report_type, str):
                return error_response("缺少报告类型")
            mode = body.get("mode", "html")
            if not isinstance(mode, str) or mode not in {"html", "image"}:
                return error_response("预览模式无效")
            service = self.giftia.reports
            info = await asyncio.to_thread(service.get_template, report_type)
            data = body.get("data", info["sample_data"])
            html = body.get("html", info["html"])
            if mode == "image":
                path = Path(await service.render_image(report_type, data, html))
                # Return bytes through the authenticated bridge, never a server file path.
                try:
                    content = await asyncio.to_thread(path.read_bytes)
                    result = {
                        "image": "data:image/jpeg;base64,"
                        + base64.b64encode(content).decode("ascii")
                    }
                finally:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
            else:
                result = {
                    "html": await asyncio.to_thread(
                        service.render_html, report_type, data, html
                    )
                }
            return json_response({"status": "success", "data": result})
        except ValueError as exc:
            return error_response(str(exc))
        except Exception:
            logger.exception("[Giftia Reports] Failed to preview report")
            return error_response(
                "预览失败，请检查 AstrBot t2i 服务及插件日志", status_code=500
            )

    async def get_report_assets(self):
        try:
            items = await asyncio.to_thread(self.giftia.reports.list_assets)
            return json_response({"status": "success", "data": items})
        except Exception:
            logger.exception("[Giftia Reports] Failed to list images")
            return error_response("读取图片素材失败", status_code=500)

    async def upload_report_asset(self):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求必须是 JSON 对象")
            item = await asyncio.to_thread(
                self.giftia.reports.upload_asset, body.get("base64")
            )
            return json_response({"status": "success", "data": item})
        except ValueError as exc:
            return error_response(str(exc))
        except Exception:
            logger.exception("[Giftia Reports] Failed to upload image")
            return error_response("上传图片失败", status_code=500)

    async def delete_report_asset(self):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return error_response("请求必须是 JSON 对象")
            name = body.get("name")
            if not isinstance(name, str) or not name:
                return error_response("缺少图片名称")
            await asyncio.to_thread(self.giftia.reports.delete_asset, name)
            return json_response({"status": "success", "message": "图片素材已删除"})
        except ValueError as exc:
            return error_response(str(exc))
        except Exception:
            logger.exception("[Giftia Reports] Failed to delete image")
            return error_response("删除图片素材失败", status_code=500)
