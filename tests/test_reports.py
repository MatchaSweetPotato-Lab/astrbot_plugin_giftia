import asyncio
import base64
from html import escape
from io import BytesIO
from pathlib import Path
from threading import Barrier, BrokenBarrierError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.reports.service import MAX_TEMPLATE_BYTES, ReportDefinition, ReportService
from core.reports.status import build_status_report
from core.utils.schemas import Status
from PIL import Image


@pytest.fixture
def service(tmp_path):
    return ReportService(
        SimpleNamespace(html_render=AsyncMock(return_value="/tmp/report.jpg")), tmp_path
    )


def test_default_report_and_text_escape(service):
    info = service.get_template("status")
    data = info["sample_data"]
    assert "思考" in info["fields"]["memory"]
    assert data["memory"]
    data["nickname"] = '<script>alert("x")</script>'
    data["custom_status"] = {"场景": "<教室>"}
    rendered = service.render_html("status", data)
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered
    assert "&lt;教室&gt;" in rendered
    assert "96%" in rendered
    assert data["memory"] in rendered
    assert "Content-Security-Policy" in rendered


@pytest.mark.parametrize("memory", [None, "", " \n\t", "<想法> & 灵感\n下一步"])
def test_status_thought_is_rendered_with_empty_fallback_and_escaping(service, memory):
    status = Status(memory=memory)
    data = build_status_report("bot", "小吉", "10001", status)
    expected = (memory or "").strip() or "暂无思考"
    assert data["memory"] == expected
    assert status.memory == memory
    rendered = service.render_html("status", data)
    assert "<h2>思考</h2>" in rendered
    assert escape(expected) in rendered
    assert "<想法>" not in rendered


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


@pytest.mark.asyncio
async def test_concurrent_uploads_enforce_limit_and_allow_existing_assets(
    service, monkeypatch
):
    raw = BytesIO()
    Image.new("RGB", (20, 20), "white").save(raw, format="WEBP")
    for index in range(99):
        (service.assets_dir / f"{index:064x}.webp").write_bytes(raw.getvalue())
    uploads = []
    for color in ("red", "blue"):
        raw = BytesIO()
        Image.new("RGB", (20, 20), color).save(raw, format="PNG")
        uploads.append(base64.b64encode(raw.getvalue()).decode())

    original_glob = Path.glob
    counted = Barrier(2, timeout=2)

    def concurrent_glob(path, pattern):
        snapshot = list(original_glob(path, pattern))
        if path == service.assets_dir and pattern == "*.webp":
            # Expose stale counts when uploads are not serialized. A protected
            # upload times out alone, then lets the next upload count its file.
            try:
                counted.wait()
            except BrokenBarrierError:
                pass
        return iter(snapshot)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "glob", concurrent_glob)
        results = await asyncio.gather(
            *(asyncio.to_thread(service.upload_asset, encoded) for encoded in uploads),
            return_exceptions=True,
        )

    succeeded = [
        index for index, result in enumerate(results) if isinstance(result, dict)
    ]
    failed = [
        index for index, result in enumerate(results) if isinstance(result, ValueError)
    ]
    assert len(succeeded) == len(failed) == 1
    assert str(results[failed[0]]) == "最多保存 100 张素材图片"
    assert len(list(service.assets_dir.glob("*.webp"))) == 100
    with pytest.raises(ValueError, match="最多保存 100 张素材图片"):
        service.upload_asset(uploads[failed[0]])
    assert service.upload_asset(uploads[succeeded[0]]) == results[succeeded[0]]
    assert len(list(service.assets_dir.glob("*.webp"))) == 100


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
def test_status_data_and_energy(energy, display, percent):
    status = SimpleNamespace(
        energy=energy,
        mood="",
        state=None,
        action="",
        custom_status={"空": " ", "服装": "水手服"},
        memory="想和大家聊聊天",
    )
    data = build_status_report("bot", "小吉", "10001", status)
    assert data["energy"] == display
    assert data["energy_percent"] == percent
    assert data["custom_status"] == {"服装": "水手服"}
    assert data["mood"] == "平稳"
    assert data["memory"] == "想和大家聊聊天"


def test_delete_asset(service, tmp_path):
    raw = BytesIO()
    Image.new("RGB", (30, 20), "blue").save(raw, format="PNG")
    asset = service.upload_asset(base64.b64encode(raw.getvalue()).decode())
    name = asset["name"]
    assert len(service.list_assets()) == 1

    service.delete_asset(name)
    assert len(service.list_assets()) == 0
    assert not (service.assets_dir / name).exists()

    with pytest.raises(ValueError, match="图片不存在"):
        service.delete_asset(name)

    with pytest.raises(ValueError, match="图片名称无效"):
        service.delete_asset("../test.webp")

