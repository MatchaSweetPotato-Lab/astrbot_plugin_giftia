"""Inventory and manual connectivity tests for configured conversation models."""

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.exceptions import EmptyModelOutputError

from ..llm.provider_health import (
    InvalidProbeResponseError,
    ProviderUnavailableError,
    get_provider_health,
)


class ModelHealthApi:
    def __init__(self, giftia):
        self.giftia = giftia

    def _used_models(self) -> list[dict]:
        """Collect configured chat provider IDs and every place they are used.

        Returns:
            Deduplicated provider descriptions with shared runtime health.
        """
        uses: dict[str, list[str]] = {}
        bots = self.giftia.bot_map
        configured_bots = getattr(
            getattr(self.giftia, "bot_config_manager", None), "bots", None
        )
        if isinstance(configured_bots, list):
            bots = {
                bot["name"]: bot
                for bot in configured_bots
                if isinstance(bot, dict) and bot.get("name")
            }
        for bot_name, bot in bots.items():
            if not bot.get("enabled", True):
                bot_name = f"{bot_name}（已停用）"
            decision = bot.get("decision_conf", {})
            reply = bot.get("llm_reply_conf", {})
            for configuration, label in (
                (decision, "前置决策"),
                (reply, "正式回复/转发转述"),
            ):
                provider_ids = configuration.get("provider_ids") or [
                    configuration.get("provider_id"),
                    *(configuration.get("fallback_provider_ids") or []),
                ]
                for provider_id in provider_ids:
                    if provider_id:
                        uses.setdefault(provider_id, []).append(f"{bot_name} · {label}")
            for provider_id in reply.get("invalid_reply_fallback_provider_ids") or []:
                if provider_id:
                    uses.setdefault(provider_id, []).append(
                        f"{bot_name} · 无效回复回退"
                    )
            if provider_id := reply.get("review_provider_id"):
                uses.setdefault(provider_id, []).append(f"{bot_name} · 回复复审")

        captions = self.giftia.caption_config
        for provider_ids, label in (
            (captions.get("media_caption_provider_ids") or [], "媒体转述"),
            (
                captions.get("inspect_media_provider_ids")
                or captions.get("media_caption_provider_ids")
                or [],
                "媒体查看",
            ),
            (self.giftia.passive_memory_provider_ids, "记忆/好感度归纳"),
        ):
            for provider_id in provider_ids:
                if provider_id:
                    uses.setdefault(provider_id, []).append(label)

        instances = getattr(
            getattr(self.giftia.context, "provider_manager", None), "inst_map", {}
        )
        health = get_provider_health(self.giftia)
        models = []
        for provider_id, labels in uses.items():
            provider = instances.get(provider_id)
            configuration = getattr(provider, "provider_config", {})
            models.append(
                {
                    **health.snapshot(provider_id),
                    "name": configuration.get("name") or provider_id,
                    "model": getattr(provider, "model_name", "")
                    or configuration.get("model", ""),
                    "registered": provider is not None,
                    "uses": list(dict.fromkeys(labels)),
                }
            )
        return models

    async def get_model_health(self):
        """List configured conversation models and their shared health."""
        return json_response(
            {
                "status": "success",
                "data": {
                    "enabled": get_provider_health(self.giftia).enabled,
                    "models": self._used_models(),
                },
            }
        )

    async def update_model_health_settings(self):
        """Persist overall health management or one configured provider's state."""
        try:
            body = await request.json()
        except ValueError:
            return error_response("请求内容格式错误")
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            return error_response("启用状态必须为布尔值")
        provider_id = body.get("provider_id")
        if "provider_id" in body and (
            not isinstance(provider_id, str)
            or provider_id
            not in {model["provider_id"] for model in self._used_models()}
        ):
            return error_response("请选择插件已配置的对话模型")
        try:
            await get_provider_health(self.giftia).update_settings(
                self.giftia.db, enabled=body["enabled"], provider_id=provider_id
            )
        except Exception:
            logger.error("Failed to save provider health settings", exc_info=True)
            return error_response("保存模型状态设置失败，请重试")
        return await self.get_model_health()

    async def test_model_connection(self):
        """Probe a used provider and restore it only after an exact challenge match."""
        try:
            body = await request.json()
        except ValueError:
            return error_response("请求内容格式错误")
        if not isinstance(body, dict):
            return error_response("请求内容格式错误")
        provider_id = body.get("provider_id")
        if not isinstance(provider_id, str) or provider_id not in {
            model["provider_id"] for model in self._used_models()
        }:
            return error_response("请选择插件已配置的对话模型")

        health = get_provider_health(self.giftia)
        if provider_id in health.disabled_provider_ids:
            return error_response("该模型已手动停用，请先启用后再测试")
        if not health.enabled:
            return error_response("模型健康管理已关闭，请先开启后再测试")
        success = False
        message = "连通性测试失败，请检查供应商配置或运行日志"
        try:
            await health.probe(
                provider_id,
                log_token_usage=self.giftia.db.log_token_usage,
                manual=True,
            )
            success = True
            message = "连通性测试通过，已恢复共享健康状态"
        except ProviderUnavailableError:
            return error_response(
                "该模型已停用、健康管理已关闭或正在进行测试，请刷新后重试"
            )
        except InvalidProbeResponseError:
            message = "测试回复未通过严格格式校验，模型保持不可用"
        except EmptyModelOutputError:
            message = "测试未收到文本响应，未恢复健康状态"
        except Exception:
            pass
        return json_response(
            {
                "status": "success" if success else "error",
                "message": message,
                "data": health.snapshot(provider_id),
            }
        )
