import json
from datetime import datetime

import mcp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor

from ..llm.native_tools import NATIVE_CONTEXT_KEY
from ..utils.schemas import MessageData


class ToolExecutor:
    def __init__(self, plugin):
        self.plugin = plugin

    async def execute_tool(
        self,
        event: AstrMessageEvent,
        bot_name: str,
        nickname: str,
        group_or_user_id: str,
        tool_name: str,
        tool_args: dict,
        tool_context: dict | None = None,
    ) -> dict:
        """执行单个 XML 工具，并在派发下一个输出前直接发送工具生成的图片。

        Args:
            event: 传递给工具并用于发送图片的事件对象。
            bot_name: 机器人配置名称。
            nickname: 写入历史记录的机器人显示名称。
            group_or_user_id: 当前会话 ID。
            tool_name: 请求调用的工具名称（可带命名空间）。
            tool_args: 解析后的工具调用参数。
            tool_context: Trusted run scope shared across tool calls and continuations.

        Returns:
            包含执行状态和文本输出的工具执行结果字典，供下一轮 LLM 循环消费。
        """
        result = []
        try:
            clean_tool_name = tool_name.split(":")[-1]
            tool_manager = self.plugin.context.get_llm_tool_manager()
            tool = tool_manager.get_func(clean_tool_name)
            if tool is None:
                tool = tool_manager.get_func(tool_name)
            if tool is None:
                logger.error(f"[Giftia] {bot_name} Tool not found: {tool_name}")
                result.append("工具不存在")
            else:
                run_context = AgentContextWrapper(
                    context=AstrAgentContext(
                        context=self.plugin.context,
                        event=event,
                        extra={
                            NATIVE_CONTEXT_KEY: json.dumps(
                                tool_context, ensure_ascii=False
                            )
                        }
                        if tool_context is not None
                        else {},
                    ),
                    tool_call_timeout=self.plugin.tools_config.get("timeout", 120),
                )
                async for tool_result in FunctionToolExecutor.execute(
                    tool, run_context, **tool_args
                ):
                    if isinstance(tool_result, str):
                        result.append(tool_result)
                    elif isinstance(tool_result, mcp.types.CallToolResult):
                        for content in tool_result.content:
                            if isinstance(content, mcp.types.TextContent):
                                result.append(content.text)
                            elif isinstance(content, mcp.types.ImageContent):
                                await event.send(
                                    MessageChain([Image.fromBase64(content.data)])
                                )
                                result.append("图片已直接发送给用户")
                if tool_context is not None:
                    tool_context.update(
                        json.loads(
                            run_context.context.extra.get(NATIVE_CONTEXT_KEY, "{}")
                        )
                    )
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            result.append(f"工具执行失败: {e}")

        tool_output = "\n".join(result)
        await self.plugin.data_cache.add_message(
            bot_name,
            group_or_user_id,
            MessageData(
                nickname=nickname,
                user_id=event.get_self_id(),
                group_or_user_id=group_or_user_id,
                time=datetime.now().isoformat(),
                message_id="",
                content=f"<tool_call name={tool_name} args={tool_args} status='finished'>\n{tool_output}\n</tool_call>",
                is_recalled=False,
                media_id_list=[],
                role="operation_log",
            ),
        )
        return {"name": tool_name, "results": tool_output}
