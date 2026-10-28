import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree.ElementTree import Element, tostring

import pytest
import pytest_asyncio
from core.conversation.reply_pipeline import ReplyPipeline
from core.conversation.tool_executor import ToolExecutor
from core.database.database import Database
from core.llm.llm_tools import GetMessageContextTool, SearchChatHistoryTool
from core.llm.xml_parse import XmlParse
from core.utils.schemas import SYSTEM_XML_TAGS, MessageData

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot.api.star.StarTools.get_data_dir", lambda *args: tmp_path
    )
    db = await Database.connect()
    try:
        # Reuse message IDs across groups and bots to verify the complete query scope.
        for bot, session in (
            ("bot", "100"),
            ("bot", "200"),
            ("bot", "300"),
            ("other-bot", "200"),
        ):
            for index, label in enumerate(("before", "target", "after")):
                await db.insert_message(
                    bot,
                    MessageData(
                        group_or_user_id=session,
                        message_id=f"shared-{label}",
                        user_id="bob" if label == "target" else "alice",
                        nickname="Member",
                        content=f"needle {bot}/{session}/{label}",
                        time=f"2026-10-28T12:0{index}:00+08:00",
                    ),
                )
        event = Mock(spec=AstrMessageEvent)
        event.platform_meta = SimpleNamespace(id="adapter")
        event.get_group_id.return_value = "100"
        event.get_sender_id.return_value = "300"
        event.get_self_id.return_value = "self"
        event.get_result.return_value = None
        context = Context.__new__(Context)
        plugin = SimpleNamespace(
            db=db,
            adapter_id_map={"adapter": "bot"},
            context=context,
            tools_config={},
            data_cache=SimpleNamespace(add_message=AsyncMock()),
        )
        tools = {
            tool.name: tool
            for tool in (
                SearchChatHistoryTool(plugin=plugin),
                GetMessageContextTool(plugin=plugin),
            )
        }
        context.get_llm_tool_manager = lambda: SimpleNamespace(get_func=tools.get)
        yield SimpleNamespace(
            event=event,
            tools=tools,
            run=SimpleNamespace(context=SimpleNamespace(event=event)),
            executor=ToolExecutor(plugin),
        )
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native", "tool_call"])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("name", ["search_chat_history", "get_message_context"])
@pytest.mark.parametrize(
    "group_id,expected_group",
    [
        (None, None),
        ("", None),
        (" \t ", None),
        ("200", "200"),
        (" 200 ", "200"),
        ("999", "999"),
    ],
)
async def test_query_scope_defaults_and_explicit_groups(
    runtime, mode, private, name, group_id, expected_group
):
    event = runtime.event
    if private:
        event.get_group_id.return_value = ""
    current_session = "300" if private else "100"
    expected_group = expected_group or current_session
    arguments = (
        {"keyword": "needle", "limit": 3}
        if name == "search_chat_history"
        else {"message_id": "shared-target", "limit": 1}
    )
    if group_id is not None:
        arguments["group_id"] = group_id
    if mode == "native":
        output = await runtime.tools[name].call(runtime.run, **arguments)
    else:
        tag = Element("tool_call", name=name)
        tag.text = json.dumps(arguments)
        parsed = await XmlParse(None, None).decode_llm_xml(
            tostring(tag, encoding="unicode"), current_session
        )
        assert parsed is not None
        [(tool_name, tool_args)] = parsed.tools_to_call
        result = await runtime.executor.execute_tool(
            event, "bot", "Giftia", current_session, tool_name, tool_args
        )
        output = result["results"]

    if expected_group == "999":
        assert "未找到" in output
    else:
        for label in ("before", "target", "after"):
            assert f"bot/{expected_group}/{label}" in output
        if name == "get_message_context":
            assert "=> [" in output
    for session in ("100", "200", "300"):
        if session != expected_group:
            assert f"bot/{session}/" not in output
    assert "other-bot/" not in output
    assert event.get_group_id() == ("" if private else "100")


@pytest.mark.asyncio
async def test_explicit_group_keeps_history_filters(runtime):
    output = await runtime.tools["search_chat_history"].call(
        runtime.run,
        group_id="200",
        keyword="needle",
        user_id="bob",
        start_time="2026-10-28T12:01:00+08:00",
        end_time="2026-10-28T12:01:00+08:00",
        sort_order="asc",
        limit=1,
    )
    assert "bot/200/target" in output
    assert "before" not in output and "after" not in output
    assert "bot/100/" not in output and "other-bot/" not in output


@pytest.mark.parametrize("tool_type", [SearchChatHistoryTool, GetMessageContextTool])
def test_group_id_is_optional_in_tool_declarations(tool_type):
    tool = tool_type()
    assert tool.parameters["properties"]["group_id"]["type"] == "string"
    assert "group_id" not in tool.parameters["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("search_chat_history", 'keyword="needle" group_id="200"'),
        ("get_message_context", 'message_id="shared-target" group_id="200"'),
        ("save_memory", 'text="obsolete"'),
        ("update_memory", 'memory_id="obsolete"'),
        ("set_relation_title", 'user_id="bob" title="obsolete"'),
    ],
)
async def test_retired_tags_do_not_query_or_trigger_a_tool_followup(name, arguments):
    result = await XmlParse(None, None).decode_llm_xml(f"<{name} {arguments}/>", "100")
    assert result is not None
    assert name not in SYSTEM_XML_TAGS
    assert not result.tools_to_call
    assert not result.output_order
    assert not ReplyPipeline._has_non_message_work(result)
    assert not hasattr(result, "search_histories")
    assert not hasattr(result, "get_message_contexts")


@pytest.mark.asyncio
async def test_retired_tags_preserve_messages_interactions_and_generic_tool_calls():
    result = await XmlParse(None, None).decode_llm_xml(
        '<search_chat_history keyword="needle"/>'
        '<get_message_context message_id="shared-target"/>'
        "<save_memory>obsolete</save_memory>"
        '<update_memory memory_id="obsolete">ignored</update_memory>'
        '<set_relation_title user_id="bob">ignored</set_relation_title>'
        "<message>Keep this reply</message>"
        '<set_status key="scene" value="home"/>'
        '<poke user_id="bob"/>'
        '<tool_call name="search_chat_history">'
        '{"group_id":"200","keyword":"needle"}</tool_call>',
        "100",
    )
    assert result is not None
    assert result.msg_chains[0][0].text == "Keep this reply"
    assert result.set_custom_status == {"scene": "home"}
    assert result.poke == [("100", "bob")]
    assert result.tools_to_call == [
        ("search_chat_history", {"group_id": "200", "keyword": "needle"})
    ]
    assert result.output_order[-1] == ("tool_call", 0)
