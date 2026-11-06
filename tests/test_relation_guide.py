from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from core.bot.bot_config_manager import BotConfigManager
from core.llm.call_llm import CallLLM
from core.llm.preset_prompts import RELATION_DELTA_GUIDE, RELATION_SCORE_GUIDE
from core.llm.xml_parse import XmlParse
from core.web.bot_api import BotApi
from core.web.profile_api import ProfileApi
from fastapi import FastAPI, Request

from astrbot.api.web import PluginRequest, bind_request_context


@pytest_asyncio.fixture
async def guidance_api(tmp_path):
    plugin = SimpleNamespace(bot_map={})
    manager = BotConfigManager.__new__(BotConfigManager)
    manager.config_file = tmp_path / "bots_config.json"
    plugin.bot_config_manager = manager
    assert manager.save_bots(
        [
            {"name": "bot-a", "llm_reply_conf": {"provider_ids": ["reply"]}},
            {"name": "bot-b", "relation_score_guide": "另一个 Bot 的说明"},
        ]
    )
    plugin.sync_bot_maps = Mock(
        side_effect=lambda: setattr(
            plugin, "bot_map", {bot["name"]: bot for bot in manager.load_bots()}
        )
    )
    plugin.sync_bot_maps()
    plugin.sync_bot_maps.reset_mock()
    api = ProfileApi()
    api.giftia = plugin
    bot_api = BotApi(plugin)
    app = FastAPI()

    @app.api_route("/guide", methods=["GET", "POST"])
    async def guidance(request: Request):
        with bind_request_context(PluginRequest(request)):
            if request.method == "GET":
                return await api.get_relation_score_guide()
            return await api.save_relation_score_guide()

    @app.post("/bot/save")
    async def save_bot(request: Request):
        with bind_request_context(PluginRequest(request)):
            return await bot_api.save_bot()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield client, plugin


@pytest.mark.asyncio
async def test_guidance_persists_per_bot_and_survives_bot_editor_saves(guidance_api):
    client, plugin = guidance_api
    initial = (await client.get("/guide", params={"bot_name": "bot-a"})).json()["data"]
    assert initial["guide"] == initial["default_guide"] == RELATION_SCORE_GUIDE
    assert (
        initial["delta_guide"] == initial["default_delta_guide"] == RELATION_DELTA_GUIDE
    )
    assert initial["tier_counts"] == initial["default_tier_counts"] == [13, 2, 2, 3, 14]
    guide = "80 分以上可主动接梗\n90 分以上可谈心；保留 {角色} 与 <边界> 原文。"
    response = await client.post("/guide", json={"bot_name": "bot-a", "guide": guide})
    assert response.status_code == 200
    plugin.sync_bot_maps.assert_called_once()
    assert plugin.bot_map["bot-a"]["relation_score_guide"] == guide
    assert plugin.bot_map["bot-a"]["llm_reply_conf"]["provider_ids"] == ["reply"]
    other = (await client.get("/guide", params={"bot_name": "bot-b"})).json()["data"]
    assert other["guide"] == "另一个 Bot 的说明"
    response = await client.post(
        "/bot/save", json={"name": "bot-a", "nickname": "新昵称"}
    )
    assert response.status_code == 200
    assert plugin.bot_map["bot-a"]["relation_score_guide"] == guide
    reloaded = (await client.get("/guide", params={"bot_name": "bot-a"})).json()["data"]
    assert reloaded["guide"] == guide


@pytest.mark.asyncio
async def test_all_guidance_settings_save_together_and_survive_other_bot_edits(
    guidance_api,
):
    client, plugin = guidance_api
    settings = {
        "bot_name": "bot-a",
        "guide": "自定义互动范围",
        "delta_guide": "具体关心记 +2；保留 {角色} 与 <行为>。",
        "tier_counts": [26, 4, 6, 9, 28],
    }
    assert (await client.post("/guide", json=settings)).status_code == 200
    current = (await client.get("/guide", params={"bot_name": "bot-a"})).json()["data"]
    for key, value in settings.items():
        assert current[key] == value
    assert (
        await client.post("/bot/save", json={"name": "bot-a", "nickname": "新昵称"})
    ).status_code == 200
    assert plugin.bot_map["bot-a"]["relation_delta_guide"] == settings["delta_guide"]
    assert plugin.bot_map["bot-a"]["relation_tier_counts"] == settings["tier_counts"]
    # A previously opened dashboard must not reset fields it does not submit.
    assert (
        await client.post("/guide", json={"bot_name": "bot-a", "guide": "只修改范围"})
    ).status_code == 200
    current = (await client.get("/guide", params={"bot_name": "bot-a"})).json()["data"]
    assert current["delta_guide"] == settings["delta_guide"]
    assert current["tier_counts"] == settings["tier_counts"]
    other = (await client.get("/guide", params={"bot_name": "bot-b"})).json()["data"]
    assert other["delta_guide"] == RELATION_DELTA_GUIDE
    assert other["tier_counts"] == [13, 2, 2, 3, 14]


@pytest.mark.asyncio
@pytest.mark.parametrize("delta_guide", ["", " \n ", RELATION_DELTA_GUIDE])
async def test_rating_guide_can_be_reset_independently(guidance_api, delta_guide):
    client, plugin = guidance_api
    response = await client.post(
        "/guide",
        json={
            "bot_name": "bot-b",
            "guide": "保留范围",
            "delta_guide": delta_guide,
            "tier_counts": [26, 4, 6, 9, 28],
        },
    )
    assert response.status_code == 200
    assert plugin.bot_map["bot-b"]["relation_delta_guide"] == ""
    current = (await client.get("/guide", params={"bot_name": "bot-b"})).json()["data"]
    assert current["delta_guide"] == RELATION_DELTA_GUIDE
    assert current["guide"] == "保留范围"
    assert current["tier_counts"] == [26, 4, 6, 9, 28]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("delta_guide", None),
        ("delta_guide", []),
        ("delta_guide", "a" * 12001),
        ("tier_counts", None),
        ("tier_counts", {}),
        ("tier_counts", [13, 2, 2, 3]),
        ("tier_counts", [True, 2, 2, 3, 14]),
        ("tier_counts", [13, 0, 2, 3, 14]),
        ("tier_counts", [13, 2, -1, 3, 14]),
        ("tier_counts", [13, 2, 2.5, 3, 14]),
        ("tier_counts", [13, 2, "2", 3, 14]),
        ("tier_counts", [13, 2, 2, 3, 1000001]),
    ],
)
async def test_invalid_rating_settings_cannot_partially_save(
    guidance_api, field, value
):
    client, plugin = guidance_api
    before = plugin.bot_config_manager.config_file.read_bytes()
    response = await client.post(
        "/guide", json={"bot_name": "bot-a", "guide": "不应写入", field: value}
    )
    assert response.status_code == 400
    assert plugin.bot_config_manager.config_file.read_bytes() == before
    plugin.sync_bot_maps.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("guide", ["", " \n ", RELATION_SCORE_GUIDE])
async def test_reset_guidance_uses_default_after_reload(guidance_api, guide):
    client, plugin = guidance_api
    response = await client.post("/guide", json={"bot_name": "bot-b", "guide": guide})
    assert response.status_code == 200
    assert plugin.bot_config_manager.load_bots()[1]["relation_score_guide"] == ""
    current = (await client.get("/guide", params={"bot_name": "bot-b"})).json()["data"]
    assert current["guide"] == RELATION_SCORE_GUIDE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"bot_name": [], "guide": "text"},
        {"bot_name": "bot-a", "guide": None},
        {"bot_name": "bot-a", "guide": ["text"]},
        {"bot_name": "bot-a", "guide": "a" * 12001},
    ],
)
async def test_invalid_guidance_does_not_replace_saved_configuration(
    guidance_api, body
):
    client, plugin = guidance_api
    before = plugin.bot_config_manager.config_file.read_bytes()
    response = await client.post("/guide", json=body)
    assert response.status_code == 400
    assert plugin.bot_config_manager.config_file.read_bytes() == before
    plugin.sync_bot_maps.assert_not_called()


@pytest.mark.asyncio
async def test_missing_bots_and_save_failures_leave_live_guidance_intact(
    guidance_api, monkeypatch
):
    client, plugin = guidance_api
    assert (await client.get("/guide")).status_code == 400
    assert (
        await client.get("/guide", params={"bot_name": "missing"})
    ).status_code == 404
    assert (
        await client.post("/guide", json={"bot_name": "missing", "guide": "text"})
    ).status_code == 404
    live_before = deepcopy(plugin.bot_map)
    manager = plugin.bot_config_manager
    memory_before = deepcopy(manager.bots)
    disk_before = manager.config_file.read_bytes()
    monkeypatch.setattr(
        "core.bot.bot_config_manager.os.replace", Mock(side_effect=OSError("disk full"))
    )
    response = await client.post(
        "/guide",
        json={
            "bot_name": "bot-b",
            "guide": "未保存",
            "delta_guide": "不应更新的标准",
            "tier_counts": [26, 4, 6, 9, 28],
        },
    )
    assert response.status_code == 500
    assert plugin.bot_map == live_before
    assert manager.bots == memory_before
    assert manager.config_file.read_bytes() == disk_before
    plugin.sync_bot_maps.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guide", ["", "自定义说明：90 分才允许主动谈心。{角色} <边界>"]
)
async def test_decision_and_reply_use_only_the_selected_bots_effective_guide(
    guidance_api, guide
):
    client, plugin = guidance_api
    assert (
        await client.post("/guide", json={"bot_name": "bot-a", "guide": guide})
    ).status_code == 200
    plugin.adapter_id_map = {"adapter-a": "bot-a"}
    context = SimpleNamespace(
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(
                completion_text='<decision reply="true" use_rag="false"/>'
            )
        ),
        tool_loop_agent=AsyncMock(
            return_value=SimpleNamespace(
                completion_text="<message>收到</message>",
                tools_call_name=[],
                reasoning_content="",
            )
        ),
    )
    caller = CallLLM(
        context,
        XmlParse(None, None),
        {"decision_retry_times": 1, "reply_retry_times": 1},
        {},
        plugin=plugin,
    )
    caller._log_token_usage_safely = AsyncMock()
    assert (
        await caller.call_llm_decision(
            ["decision"], "角色设定", "待处理消息", bot_name="bot-a"
        )
        is not None
    )
    event = SimpleNamespace(platform_meta=SimpleNamespace(id="adapter-a"))
    for enabled_features, tool_context in ((None, None), ([], {"bot_name": "bot-a"})):
        assert (
            await caller.call_llm_reply(
                event,
                "group",
                ["reply"],
                "角色设定",
                "待回复消息",
                enabled_features=enabled_features,
                tool_context=tool_context,
            )
            is not None
        )
    expected = guide or RELATION_SCORE_GUIDE
    prompts = [
        context.llm_generate.call_args.kwargs["system_prompt"],
        *[
            call.kwargs["system_prompt"]
            for call in context.tool_loop_agent.call_args_list
        ],
    ]
    for prompt in prompts:
        assert prompt.count(expected) == 1
        assert "另一个 Bot 的说明" not in prompt
        if guide:
            assert RELATION_SCORE_GUIDE not in prompt
