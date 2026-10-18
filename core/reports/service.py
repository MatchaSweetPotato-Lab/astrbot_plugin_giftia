import asyncio
import base64
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .user_profile import USER_PROFILE_FIELDS, build_user_profile_report

MAX_TEMPLATE_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 12 * 1024 * 1024
REPORT_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "font-src data:; base-uri 'none'; form-action 'none'"
)


@dataclass(frozen=True)
class ReportDefinition:
    """Metadata needed to add a report type to the shared editor and renderer."""

    label: str
    template: Path
    sample: dict
    fields: dict[str, str]


class ReportService:
    def __init__(self, plugin, data_dir: Path):
        self.plugin = plugin
        self.root = Path(data_dir) / "reports"
        self.assets_dir = self.root / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self._asset_lock = Lock()
        self.definitions: dict[str, ReportDefinition] = {}
        self.environment = ImmutableSandboxedEnvironment(
            autoescape=True, undefined=StrictUndefined
        )
        self.environment.globals.clear()
        self.register(
            "status",
            ReportDefinition(
                label="状态报告",
                template=Path(__file__).parent / "templates" / "status.html",
                sample={
                    "report_type": "status",
                    "title": "Bot 状态看板",
                    "bot_name": "Giftia",
                    "nickname": "小吉",
                    "session_id": "10001",
                    "generated_at": "2026-09-06 14:30:00 CST",
                    "mood": "开心",
                    "state": "空闲",
                    "action": "品茶",
                    "memory": "茶香刚好，想和大家聊聊今天发生的趣事。",
                    "energy": "96%",
                    "energy_percent": 96,
                    "custom_status": {"服装": "水手服", "场景": "教室"},
                },
                fields={
                    "title": "报告标题",
                    "bot_name": "机器人名称",
                    "nickname": "机器人昵称",
                    "session_id": "会话 ID",
                    "generated_at": "生成时间",
                    "mood": "心情",
                    "state": "状态",
                    "action": "动作",
                    "memory": "当前会话的思考内容（为空时显示「暂无思考」）",
                    "energy": "能量文本（含 %）",
                    "energy_percent": "0–100 的能量数值",
                    "custom_status": "常驻状态字典",
                    "report_type": "报告类型标识",
                },
            ),
        )

        self.register(
            "user_profile",
            ReportDefinition(
                label="用户画像",
                template=Path(__file__).parent / "templates" / "user_profile.html",
                sample={
                    **build_user_profile_report(
                        "Giftia",
                        "小吉",
                        "10001",
                        "123456789",
                        {
                            "call_name": "小明",
                            "aliases": "阿明，明同学",
                            "attitude": "熟络，愿意认真回应彼此",
                            "avatar_description": "蓝色天空下的一只白猫。",
                            "relation": 68,
                            "base_relation": 5,
                            "interaction_score": 63,
                            "recent_score": 60,
                            "long_term_score": 67.5,
                            "total_count": 35,
                            "recent_count": 10,
                        },
                        recent_relation_changes=[
                            {
                                "delta": 2,
                                "relation_before": 66,
                                "relation_after": 68,
                                "reason": "他主动关心我的近况，我表达了感谢",
                                "occurred_at": "2026-10-18T14:00:00+08:00",
                            },
                            {
                                "delta": -1,
                                "relation_before": 67,
                                "relation_after": 66,
                                "reason": "他反复打断我的解释，我提醒了交流边界",
                                "occurred_at": "2026-10-17T15:30:00+08:00",
                            },
                            {
                                "delta": 1,
                                "relation_before": 66,
                                "relation_after": 67,
                                "reason": "他分享今天的趣事，我顺着话题聊了几句",
                                "occurred_at": "2026-10-16T12:00:00+08:00",
                            },
                        ],
                    ),
                    "generated_at": "2026-10-18 14:30:00 CST",
                },
                fields={
                    "title": "报告标题",
                    "bot_name": "机器人名称",
                    "nickname": "机器人昵称",
                    "session_id": "会话 ID",
                    "user_id": "查询用户 ID",
                    "generated_at": "生成时间",
                    **{
                        field: f"{label}；无记录时为空字符串"
                        for field, label in USER_PROFILE_FIELDS.items()
                    },
                    "relation": "最终好感度=基础分+互动分，显示一位小数；0 是有效分值",
                    "base_relation": "管理员设置的基础分，默认0，可为负数",
                    "interaction_score": "互动分，初始50、范围0～100；近期分×60%+长期分×40%",
                    "recent_score": "最近最多10次互动评价按幅度加权并平滑后的分值",
                    "long_term_score": "全部已记录互动评价按幅度加权并平滑后的分值",
                    "total_count": "全部互动评价条数",
                    "recent_count": "近期样本数，最多10条",
                    "profile_fields": "非空画像字段字典（中文标签 → 展示文本）；空字段不包含在内",
                    "recent_relation_changes": "最近最多三次互动评价，最新在前；展示delta（评价强度）、reason和time；另含occurred_at、created_at；无历史时为空数组",
                    "report_type": "报告类型标识",
                },
            ),
        )

    def register(self, report_type: str, definition: ReportDefinition) -> None:
        """Register a report type for both dashboard editing and command rendering.

        Args:
            report_type: Stable lowercase identifier used in storage and API calls.
            definition: Default template, sample data and field descriptions.

        Raises:
            ValueError: If the identifier is invalid or already registered.
        """
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", report_type)
            or report_type in self.definitions
        ):
            raise ValueError("报告类型无效或重复")
        self.definitions[report_type] = definition

    def get_template(self, report_type: str) -> dict:
        if report_type not in self.definitions:
            raise ValueError("未知的报告类型")
        definition = self.definitions[report_type]
        default = definition.template.read_text(encoding="utf-8")
        custom = self.root / f"{report_type}.html"
        return {
            "report_type": report_type,
            "label": definition.label,
            "html": custom.read_text(encoding="utf-8") if custom.exists() else default,
            "default_html": default,
            "customized": custom.exists(),
            "sample_data": deepcopy(definition.sample),
            "fields": definition.fields,
        }

    def render_html(self, report_type: str, data: dict, html: str | None = None) -> str:
        """Render a sandboxed template once for both browser preview and t2i.

        Args:
            report_type: Registered report identifier.
            data: JSON-compatible report data.
            html: Optional unsaved template for preview.

        Returns:
            HTML with embedded assets and a restrictive content security policy.

        Raises:
            ValueError: If the template, data or an asset is invalid.
        """
        if report_type not in self.definitions:
            raise ValueError("未知的报告类型")
        if html is None:
            html = self.get_template(report_type)["html"]
        if (
            not isinstance(html, str)
            or not html.strip()
            or len(html.encode()) > MAX_TEMPLATE_BYTES
        ):
            raise ValueError("HTML 模板不能为空且不能超过 256 KB")
        if (
            not isinstance(data, dict)
            or len(json.dumps(data).encode()) > MAX_TEMPLATE_BYTES
        ):
            raise ValueError("预览数据必须是 JSON 对象且不能超过 256 KB")
        try:
            template = self.environment.from_string(html)
            parts = []
            size = 0
            for part in template.generate({**data, "asset": self.asset_url}):
                size += len(part.encode())
                if size > MAX_HTML_BYTES:
                    raise ValueError("报告内容过大，请减少图片或循环内容")
                parts.append(part)
            # The leading policy also applies to complete user-supplied HTML documents.
            return (
                f'<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{REPORT_CSP}">'
                + "".join(parts)
            )
        except TemplateError as exc:
            raise ValueError(f"模板错误：{exc}") from exc

    def save_template(self, report_type: str, html: str) -> None:
        # Validate against the public schema before replacing the last usable template.
        info = self.get_template(report_type)
        self.render_html(report_type, info["sample_data"], html)
        target = self.root / f"{report_type}.html"
        temporary = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.root, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(html)
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def asset_url(self, name: str) -> str:
        if not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{64}\.webp", name):
            raise ValueError("图片名称无效")
        path = self.assets_dir / name
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > MAX_IMAGE_BYTES
        ):
            raise ValueError("图片不存在或过大")
        return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode(
            "ascii"
        )

    def upload_asset(self, encoded: str) -> dict:
        from PIL import Image, UnidentifiedImageError

        if not isinstance(encoded, str) or len(encoded) > (
            MAX_IMAGE_BYTES * 4 // 3 + 4
        ):
            raise ValueError("图片不能超过 2 MB")
        try:
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                raise ValueError("图片不能为空且不能超过 2 MB")
            with Image.open(BytesIO(raw)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                    raise ValueError("仅支持 PNG、JPEG、WebP、GIF 图片")
                if source.width * source.height > 16_000_000:
                    raise ValueError("图片不能超过 1600 万像素")
                # Re-encode the first frame to strip active content and metadata.
                output = BytesIO()
                source.convert("RGBA").save(output, format="WEBP", quality=90)
                content = output.getvalue()
            if len(content) > MAX_IMAGE_BYTES:
                raise ValueError("处理后的图片超过 2 MB，请缩小图片")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("无法读取图片，请上传有效的图片文件") from exc
        name = sha256(content).hexdigest() + ".webp"
        path = self.assets_dir / name
        # Uploads run in worker threads; keep deduplication and quota checks together.
        with self._asset_lock:
            if not path.exists():
                if len(list(self.assets_dir.glob("*.webp"))) >= 100:
                    raise ValueError("最多保存 100 张素材图片")
                path.write_bytes(content)
        return {"name": name, "size": len(content), "url": self.asset_url(name)}

    def delete_asset(self, name: str) -> None:
        if not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{64}\.webp", name):
            raise ValueError("图片名称无效")
        path = self.assets_dir / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("图片不存在")
        with self._asset_lock:
            path.unlink(missing_ok=True)

    def list_assets(self) -> list[dict]:
        from PIL import Image

        items = []
        for path in sorted(self.assets_dir.glob("*.webp")):
            if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{64}\.webp", path.name):
                continue
            with Image.open(path) as source:
                source.thumbnail((160, 120))
                output = BytesIO()
                source.save(output, format="WEBP", quality=75)
            items.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "url": "data:image/webp;base64,"
                    + base64.b64encode(output.getvalue()).decode("ascii"),
                }
            )
        return items

    async def render_image(
        self, report_type: str, data: dict, html: str | None = None
    ) -> str:
        rendered = await asyncio.to_thread(self.render_html, report_type, data, html)
        # Pass rendered HTML as data so literal Jinja expressions are never evaluated twice.
        return await asyncio.wait_for(
            self.plugin.html_render(
                "{{ report_html | safe }}",
                {"report_html": rendered},
                return_url=False,
                options={"full_page": True, "type": "jpeg", "quality": 90},
            ),
            timeout=45,
        )
