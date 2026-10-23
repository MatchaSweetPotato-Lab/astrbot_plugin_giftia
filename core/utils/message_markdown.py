import re
from html import unescape
from itertools import groupby
from urllib.parse import parse_qs

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain
from astrbot.core.message.components import BaseMessageComponent

_QQ_MENTION = re.compile(
    r"\[(?P<label>(?:\\.|[^\]\\])*)\](?P<escape>\\?)"
    r"\(mqqapi://markdown/mention\?(?P<query>[^()\s]*?)(?P=escape)\)"
)
_MENTION_SPACING = re.compile(r"\s*(\x00[0-9]+\x00)\s*")


def normalize_qq_markdown_chain(
    chain: list[BaseMessageComponent],
    *,
    event: AstrMessageEvent | None = None,
) -> list[BaseMessageComponent]:
    """Normalize QQ mentions and remove identical adjacent fallback copies.

    Args:
        chain: Components that may include Markdown flattened by the adapter.
        event: Receiving event providing raw OneBot segment types. Its source is
            used only for its own unmodified root chain, never outgoing or quoted
            chains parsed in the same event context.

    Returns:
        A local chain with readable mentions, including escaped link delimiters.
        An adjacent raw text fallback is omitted only when its complete text
        matches a Markdown segment. Native mention components take precedence
        over equivalent Markdown mentions. Original components are not modified.
    """
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    get_messages = getattr(event, "get_messages", None)
    if (
        isinstance(raw, dict)
        and raw.get("post_type") == "message"
        and isinstance(raw.get("message"), list)
        and callable(get_messages)
        and chain is get_messages()
    ):
        # Mirror the adapter's text grouping, then require an exact alignment
        # before assigning source types to Plain components. Other plugins may
        # have changed the chain after the adapter populated raw_message.
        sources = []
        for kind, segments in groupby(raw["message"], key=lambda seg: seg["type"]):
            if kind == "text":
                text = "".join(seg["data"]["text"] for seg in segments).strip()
                if text:
                    sources.append((kind, text))
            elif kind == "markdown":
                for seg in segments:
                    data = seg["data"]
                    sources.append(
                        (kind, data.get("markdown") or data.get("content", ""))
                    )
        plain_indices = [i for i, comp in enumerate(chain) if isinstance(comp, Plain)]
        if [chain[i].text for i in plain_indices] == [text for _, text in sources]:
            origins = dict(zip(plain_indices, (kind for kind, _ in sources)))
            fallbacks = set()
            for left, right in zip(plain_indices, plain_indices[1:]):
                if (
                    right == left + 1
                    and {origins[left], origins[right]} == {"markdown", "text"}
                    and chain[left].text.strip() == chain[right].text.strip()
                ):
                    fallbacks.add(left if origins[left] == "text" else right)
            chain = [comp for i, comp in enumerate(chain) if i not in fallbacks]

    entries = []
    markdown_indices = set()
    for comp in chain:
        expanded = []
        end = 0
        if isinstance(comp, Plain):
            for match in _QQ_MENTION.finditer(comp.text):
                params = parse_qs(unescape(match["query"]))
                target = params.get("at_tinyid", [""])[0]
                if params.get("at_type") != ["1"] or not re.fullmatch(
                    r"[0-9]+", target
                ):
                    continue
                if match.start() > end:
                    expanded.append(Plain(comp.text[end : match.start()]))
                name = re.sub(r"\\([^\w\s])", r"\1", unescape(match["label"]))
                expanded.append(At(qq=target, name=name.removeprefix("@")))
                end = match.end()
            if expanded:
                if end < len(comp.text):
                    expanded.append(Plain(comp.text[end:]))
                markdown_indices.add(len(entries))
        entries.append(expanded or [comp])

    if not markdown_indices:
        return list(chain)

    # Compare IDs rather than nicknames: group cards can differ from Markdown
    # labels. Null-delimited IDs also keep ordinary repeated text distinct.
    keys = [
        "".join(
            f"\x00{comp.qq}\x00" if isinstance(comp, At) else comp.text
            for comp in entry
        )
        if all(isinstance(comp, (Plain, At)) for comp in entry)
        else None
        for entry in entries
    ]
    omitted = set()
    for index in sorted(markdown_indices):
        expected = _MENTION_SPACING.sub(r"\1", keys[index]).strip()
        plain_body = re.sub(r"\x00[0-9]+\x00", "", keys[index]).strip()
        for direction in (1, -1):
            position = index + direction
            plain_copy_index = None
            # Some messages also contain a separate plain-text fallback before
            # the native mention. Remove it only after the complete native copy
            # matches; repetition inside the Markdown itself stays untouched.
            if (
                direction == 1
                and position < len(entries)
                and len(entries[position]) == 1
                and isinstance(entries[position][0], Plain)
                and plain_body
                and keys[position].strip() == plain_body
            ):
                plain_copy_index = position
                position += 1
            adjacent = ""
            while 0 <= position < len(entries):
                if position in markdown_indices or keys[position] is None:
                    break
                adjacent = (
                    adjacent + keys[position]
                    if direction == 1
                    else keys[position] + adjacent
                )
                actual = _MENTION_SPACING.sub(r"\1", adjacent).strip()
                if actual == expected:
                    omitted.add(index)
                    if plain_copy_index is not None:
                        omitted.add(plain_copy_index)
                    break
                if not (
                    expected.startswith(actual)
                    if direction == 1
                    else expected.endswith(actual)
                ):
                    break
                position += direction
            if index in omitted:
                break

    return [
        comp
        for index, entry in enumerate(entries)
        if index not in omitted
        for comp in entry
    ]
