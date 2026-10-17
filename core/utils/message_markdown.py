import re
from html import unescape
from urllib.parse import parse_qs

from astrbot.api.message_components import At, Plain
from astrbot.core.message.components import BaseMessageComponent

_QQ_MENTION = re.compile(
    r"\[((?:\\.|[^\]\\])*)\]\(mqqapi://markdown/mention\?([^()\s]*)\)"
)
_MENTION_SPACING = re.compile(r"\s*(\x00[0-9]+\x00)\s*")


def normalize_qq_markdown_chain(
    chain: list[BaseMessageComponent],
) -> list[BaseMessageComponent]:
    """Normalize QQ mentions and remove an identical adjacent fallback copy.

    Args:
        chain: Components that may include Markdown flattened by the adapter.

    Returns:
        A local chain with readable mentions. A Markdown component is omitted
        only when adjacent ordinary components reproduce its full text and
        mention IDs. Original components are never modified.
    """
    entries = []
    markdown_indices = set()
    for comp in chain:
        expanded = []
        end = 0
        if isinstance(comp, Plain):
            for match in _QQ_MENTION.finditer(comp.text):
                params = parse_qs(unescape(match[2]))
                target = params.get("at_tinyid", [""])[0]
                if params.get("at_type") != ["1"] or not re.fullmatch(
                    r"[0-9]+", target
                ):
                    continue
                if match.start() > end:
                    expanded.append(Plain(comp.text[end : match.start()]))
                name = re.sub(r"\\([^\w\s])", r"\1", unescape(match[1]))
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
    for index in markdown_indices:
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
