import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.reports.service import MAX_TEMPLATE_BYTES, ReportDefinition, ReportService
from core.reports.status import build_status_report
from PIL import Image


@pytest.fixture
def service(tmp_path):
    return ReportService(
        SimpleNamespace(html_render=AsyncMock(return_value="/tmp/report.jpg")), tmp_path
    )


def test_default_report_and_text_escape(service):
    data = service.get_template("status")["sample_data"]
    data["nickname"] = '<script>alert("x")</script>'
    data["custom_status"] = {"场景": "<教室>"}
    rendered = service.render_html("status", data)
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered
    assert "&lt;教室&gt;" in rendered
    assert "96%" in rendered
    assert "Content-Security-Policy" in rendered


@pytest.mark.parametrize(
    "html",
    [
        "",
        "{% if %}",
        "{{ missing }}",
        "{{ nickname.__class__.__mro__ }}",
        "x" * (MAX_TEMPLATE_BYTES + 1),
    ],
)
def test_invalid_template_does_not_replace_saved_template(service, html):
    service.save_template("status", "<h1>{{ nickname }}</h1>")
    with pytest.raises(ValueError):
        service.save_template("status", html)
    assert service.get_template("status")["html"] == "<h1>{{ nickname }}</h1>"


def test_templates_survive_restart_and_report_types_are_isolated(service, tmp_path):
    service.save_template("status", "<p>{{ mood }}</p>")
    restarted = ReportService(None, tmp_path)
    assert restarted.get_template("status")["html"] == "<p>{{ mood }}</p>"
    profile_template = tmp_path / "profile_default.html"
    profile_template.write_text("<h1>{{ name }}</h1>")
    restarted.register(
        "custom_report",
        ReportDefinition(
            "用户画像", profile_template, {"name": "小明"}, {"name": "昵称"}
        ),
    )
    restarted.save_template("custom_report", "<p>{{ name }}</p>")
    assert "小明" in restarted.render_html("custom_report", {"name": "小明"})
    assert restarted.get_template("status")["html"] == "<p>{{ mood }}</p>"
    with pytest.raises(ValueError):
        restarted.get_template("../../secret")


def test_uploaded_image_is_validated_and_embedded_for_remote_t2i(service, tmp_path):
    raw = BytesIO()
    Image.new("RGB", (30, 20), "red").save(raw, format="PNG")
    asset = service.upload_asset(base64.b64encode(raw.getvalue()).decode())
    assert asset["name"].endswith(".webp")
    template = "<img src=\"{{ asset('" + asset["name"] + "') }}\">"
    service.save_template("status", template)
    rendered = service.render_html("status", {})
    assert "data:image/webp;base64," in rendered
    assert str(tmp_path) not in rendered
    assert service.list_assets()[0]["name"] == asset["name"]
    restarted = ReportService(None, tmp_path)
    assert restarted.render_html("status", {}) == rendered
    assert (
        service.upload_asset(base64.b64encode(raw.getvalue()).decode())["name"]
        == asset["name"]
    )
    assert len(service.list_assets()) == 1


@pytest.mark.parametrize(
    "encoded",
    [
        None,
        "!invalid",
        base64.b64encode(b"<svg><script/></svg>").decode(),
        "A" * 3_000_000,
    ],
)
def test_invalid_upload_rejected(service, encoded):
    with pytest.raises(ValueError):
        service.upload_asset(encoded)
    assert not list(service.assets_dir.iterdir())


def test_asset_path_traversal_and_symlinks_rejected(service, tmp_path):
    for name in ("../secret", "/etc/passwd", "a.webp", None):
        with pytest.raises(ValueError):
            service.asset_url(name)
    secret = tmp_path / "secret"
    secret.write_bytes(b"secret")
    asset_name = "a" * 64 + ".webp"
    (service.assets_dir / asset_name).symlink_to(secret)
    with pytest.raises(ValueError):
        service.asset_url(asset_name)


@pytest.mark.asyncio
async def test_image_render_uses_astrbot_t2i_without_second_jinja_evaluation(service):
    data = service.get_template("status")["sample_data"]
    data["nickname"] = "{{ 7 * 7 }}"
    path = await service.render_image("status", data)
    assert path == "/tmp/report.jpg"
    call = service.plugin.html_render.call_args
    assert call.args[0] == "{{ report_html | safe }}"
    assert "{{ 7 * 7 }}" in call.args[1]["report_html"]
    assert call.kwargs["return_url"] is False
    assert call.kwargs["options"]["full_page"] is True


@pytest.mark.parametrize(
    ("energy", "display", "percent"),
    [
        (None, "100%", 100),
        ("95.5%", "96%", 96),
        ("0", "0%", 0),
        ("150", "100%", 100),
        ("nan", "100%", 100),
        ("abc", "100%", 100),
    ],
)
def test_status_public_data_and_energy(energy, display, percent):
    status = SimpleNamespace(
        energy=energy,
        mood="",
        state=None,
        action="",
        custom_status={"空": " ", "服装": "水手服"},
        memory="private",
    )
    data = build_status_report("bot", "小吉", "10001", status)
    assert data["energy"] == display
    assert data["energy_percent"] == percent
    assert data["custom_status"] == {"服装": "水手服"}
    assert data["mood"] == "平稳"
    assert "memory" not in data
    assert "private" not in str(data)
