import json
import re
from html import unescape
from urllib.parse import urlsplit

from astrbot.api.message_components import Image, Plain
from astrbot.core.message.components import BaseMessageComponent


def json_card_to_components(data: str | dict) -> list[BaseMessageComponent]:
    """Expand a JSON miniapp card into readable text and ordinary images.

    Args:
        data: An Ark JSON payload, optionally wrapped in OneBot's data field.

    Returns:
        Text and image components in card order, or a readable fallback.
    """
    # Bound unwrapping so malformed nested payloads cannot loop indefinitely.
    for _ in range(4):
        if isinstance(data, str):
            try:
                data = json.loads(data.strip().replace("&#44;", ","))
            except (ValueError, TypeError):
                return [Plain("[JSON卡片]")]
        if not isinstance(data, dict):
            return [Plain("[JSON卡片]")]
        if "app" in data or "data" not in data:
            break
        data = data["data"]
    if not isinstance(data, dict):
        return [Plain("[JSON卡片]")]

    app = data.get("app")
    if app == "com.tencent.multimsg" or data.get("view") in ("contact", "Forward"):
        return [Plain("[合并转发消息]")]

    is_miniapp = isinstance(app, str) and app.startswith("com.tencent.miniapp")
    if not is_miniapp:
        summary = next(
            (
                unescape(value.strip())
                for key in ("prompt", "desc")
                if isinstance(value := data.get(key), str) and value.strip()
            ),
            "",
        )
        return [Plain(f"[JSON卡片]\n{summary}".strip())]

    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    detail = meta.get("detail_1")
    if not isinstance(detail, dict):
        detail = next(
            (
                value
                for key, value in meta.items()
                if (key == "detail" or key.startswith("detail_"))
                and isinstance(value, dict)
            ),
            {},
        )

    texts = []
    for value in (data.get("desc"), detail.get("title"), detail.get("desc")):
        if isinstance(value, str) and value.strip():
            text = unescape(value.strip())
            if text not in texts:
                texts.append(text)
    if not texts:
        prompt = data.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            texts.append(unescape(prompt.strip()))

    # QQ previews and miniapp links may omit their scheme. Only remote web
    # references belong here; card fields must not become local file reads.
    urls = {}
    for key in ("preview", "qqdocurl", "url"):
        value = detail.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        url = unescape(value.strip())
        if url.startswith("//"):
            url = "https:" + url
        elif re.match(r"^[^/:\s]+\.[^/:\s]+(?:/|$)", url):
            url = "https://" + url
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and not any(char.isspace() for char in url)
            ):
                urls[key] = url
        except ValueError:
            continue

    components = [Plain("\n".join(["[小程序]", *texts]))]
    if preview := urls.get("preview"):
        components.append(Image.fromURL(preview))
    if link := urls.get("qqdocurl") or urls.get("url"):
        components.append(Plain(f"\n链接: {link}"))
    return components
