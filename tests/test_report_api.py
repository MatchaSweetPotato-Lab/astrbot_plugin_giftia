import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from core.reports.service import ReportService
from PIL import Image


@pytest.fixture
def api_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRBOT_ROOT", str(tmp_path / "runtime"))
    from core.web.report_api import ReportApi
    from fastapi import FastAPI, Request

    from astrbot.api.web import PluginRequest, bind_request_context

    plugin = SimpleNamespace(html_render=AsyncMock())
    plugin.reports = ReportService(plugin, tmp_path)
    api = ReportApi()
    api.giftia = plugin
    app = FastAPI()

    @app.api_route("/{endpoint:path}", methods=["GET", "POST"])
    async def dispatch(endpoint: str, request: Request):
        handlers = {
            "templates": api.get_report_templates,
            "save": api.update_report_template,
            "preview": api.preview_report,
            "assets": api.get_report_assets,
            "upload": api.upload_report_asset,
        }
        with bind_request_context(PluginRequest(request)):
            return await handlers[endpoint]()

    return app, plugin


@pytest.mark.asyncio
async def test_editor_save_preview_and_validation(api_app):
    app, plugin = api_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get("/templates")
        assert response.status_code == 200
        assert response.json()["data"][0]["report_type"] == "status"
        assert [entry["report_type"] for entry in response.json()["data"]] == [
            "status",
            "user_profile",
        ]
        body = {
            "report_type": "status",
            "html": "<b>{{ nickname }}</b>",
            "data": {"nickname": "预览用户"},
        }
        preview = await client.post("/preview", json=body)
        assert "<b>预览用户</b>" in preview.json()["data"]["html"]
        assert "<b>" not in plugin.reports.get_template("status")["html"]
        saved = await client.post("/save", json=body)
        assert saved.status_code == 200
        assert plugin.reports.get_template("status")["html"] == body["html"]
        for invalid in (
            [],
            None,
            {"report_type": []},
            {"report_type": "../../secret"},
            {"report_type": "status", "html": "{{ nonexistent }}"},
        ):
            failed = await client.post("/save", json=invalid)
            assert failed.status_code == 400
        assert plugin.reports.get_template("status")["html"] == body["html"]


@pytest.mark.asyncio
@pytest.mark.parametrize("report_type", ["status", "user_profile"])
async def test_api_image_upload_and_embedding(api_app, report_type):
    app, _ = api_app
    raw = BytesIO()
    Image.new("RGB", (12, 12), "green").save(raw, format="PNG")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        upload = await client.post(
            "/upload", json={"base64": base64.b64encode(raw.getvalue()).decode()}
        )
        assert upload.status_code == 200
        name = upload.json()["data"]["name"]
        listing = await client.get("/assets")
        assert listing.json()["data"][0]["name"] == name
        result = await client.post(
            "/preview",
            json={"report_type": report_type, "html": "{{ asset('" + name + "') }}"},
        )
        assert "data:image/webp;base64," in result.json()["data"]["html"]
        invalid = await client.post("/upload", json={"base64": "invalid"})
        assert invalid.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("report_type", ["status", "user_profile"])
async def test_image_preview_cleanup_and_t2i_failure(api_app, tmp_path, report_type):
    app, plugin = api_app
    image_path = tmp_path / "render.jpg"
    Image.new("RGB", (12, 12), "green").save(image_path)
    plugin.html_render.return_value = str(image_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/preview", json={"report_type": report_type, "mode": "image"}
        )
        assert response.status_code == 200
        assert response.json()["data"]["image"].startswith("data:image/jpeg;base64,")
        assert not image_path.exists()
        plugin.html_render.side_effect = RuntimeError("private internal endpoint")
        response = await client.post(
            "/preview", json={"report_type": report_type, "mode": "image"}
        )
        assert response.status_code == 500
        assert "private internal endpoint" not in response.text


@pytest.mark.asyncio
async def test_user_profile_template_save_preview_and_restart(api_app, tmp_path):
    app, plugin = api_app
    status_html = plugin.reports.get_template("status")["html"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        preview = await client.post("/preview", json={"report_type": "user_profile"})
        assert preview.status_code == 200
        assert "用户画像" in preview.json()["data"]["html"]
        assert "小明" in preview.json()["data"]["html"]
        body = {
            "report_type": "user_profile",
            "html": "<h1>{{ user_id }} · {{ call_name }}</h1>",
        }
        saved = await client.post("/save", json=body)
        assert saved.status_code == 200
        restarted = ReportService(None, tmp_path)
        assert restarted.get_template("user_profile")["html"] == body["html"]
        assert restarted.get_template("status")["html"] == status_html
