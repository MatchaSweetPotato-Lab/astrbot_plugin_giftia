"""Shared, in-memory health tracking at the model request boundary."""

import asyncio
import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

from astrbot.api import logger
from astrbot.core.exceptions import EmptyModelOutputError

from ..utils.token_utils import extract_tokens_robust


class ProviderUnavailableError(Exception):
    """Signal a skipped provider without making a model request."""


@dataclass
class _HealthState:
    signature: str | None = None
    failures: int = 0
    retry_at: float = 0
    backoff: int = 0
    generation: int = 0
    probing: bool = False
    last_attempt: float | None = None
    last_success: float | None = None
    last_failure: float | None = None
    last_latency_ms: int | None = None
    reason: str = ""


class ProviderHealth:
    """Share cooldown and recovery probes across one plugin's model calls."""

    COOLDOWNS = (60, 120, 300, 600)
    FAILURE_THRESHOLD = 2
    CHECK_INTERVAL = 1
    PROBE_TIMEOUT = 30

    def __init__(self, context, *, clock: Callable[[], float] = time.monotonic):
        self.context = context
        self.clock = clock
        self._states: dict[str, _HealthState] = {}
        self._monitor_task: asyncio.Task | None = None

    def start(self, log_token_usage: Callable[..., Awaitable[Any]]):
        """Start independent recovery checks for cooling providers.

        Args:
            log_token_usage: Token usage recorder for background test requests.
        """
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor(log_token_usage))

    async def close(self):
        """Cancel the monitor and await its outstanding model probes."""
        task, self._monitor_task = self._monitor_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _monitor(self, log_token_usage: Callable[..., Awaitable[Any]]):
        """Schedule due probes without making business calls await recovery.

        Each provider has its own task so a slow endpoint cannot delay another
        provider's recovery. The request boundary owns the shared probe lock.

        Args:
            log_token_usage: Token usage recorder for automatic probes.
        """
        probes: dict[str, asyncio.Task] = {}
        try:
            while True:
                for provider_id, task in list(probes.items()):
                    if task.done():
                        if not task.cancelled():
                            task.exception()
                        del probes[provider_id]
                for provider_id, state in list(self._states.items()):
                    if (
                        state.retry_at
                        and state.retry_at <= self.clock()
                        and not state.probing
                        and provider_id not in probes
                    ):
                        probes[provider_id] = asyncio.create_task(
                            self.probe(provider_id, log_token_usage=log_token_usage)
                        )
                await asyncio.sleep(self.CHECK_INTERVAL)
        finally:
            for task in probes.values():
                task.cancel()
            await asyncio.gather(*probes.values(), return_exceptions=True)

    async def probe(
        self,
        provider_id: str,
        *,
        log_token_usage: Callable[..., Awaitable[Any]],
        manual: bool = False,
    ):
        """Test connectivity and record the same result for manual and auto probes.

        Args:
            provider_id: AstrBot provider ID to test independently of business.
            log_token_usage: Token usage recorder for the actual test request.
            manual: Whether the request was initiated from the WebUI.

        Returns:
            The SDK response after a successful, nonempty text reply.

        Raises:
            ProviderUnavailableError: Another probe owns this provider's slot.
            Exception: The original request error or an empty test response.
        """
        response = None
        error = None
        try:
            response = await self.call(
                self.context.llm_generate,
                chat_provider_id=provider_id,
                prompt="请只回复 OK。",
                request_timeout=self.PROBE_TIMEOUT,
                force_probe=True,
            )
        except ProviderUnavailableError:
            raise
        except Exception as exc:
            error = exc
            logger.warning(
                "Provider connectivity probe failed. provider_id: %s, manual: %s, error: %s",
                provider_id,
                manual,
                exc,
            )
        try:
            prompt_tokens, completion_tokens, total_tokens = extract_tokens_robust(
                response
            )
            await log_token_usage(
                bot_name="",
                group_or_user_id="",
                type="provider_test",
                provider_id=provider_id,
                model_name=provider_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                extra_info={
                    "status": "api_failed" if error is not None else "success",
                    "manual_test": manual,
                },
            )
        except Exception:
            logger.warning(
                "Failed to record provider probe usage. provider_id: %s",
                provider_id,
                exc_info=True,
            )
        if error is not None:
            raise error
        return response

    async def call(
        self,
        operation: Callable[..., Awaitable[Any]],
        *,
        chat_provider_id: str,
        request_timeout: float | None = None,
        force_probe: bool = False,
        count_failure: Callable[[], bool] | None = None,
        **kwargs,
    ):
        """Run one SDK call with request retries controlled by the caller.

        State transitions do not await, so admission and probe ownership are
        atomic on the plugin event loop. Generations ignore results from requests
        admitted before a cooldown, configuration change or recovery probe.

        Args:
            operation: AstrBot generation or native tool-loop method.
            chat_provider_id: Shared AstrBot provider configuration ID.
            request_timeout: Optional timeout around the actual model operation.
            force_probe: Bypass cooldown for an independent connectivity test.
            count_failure: Whether an exception can still be attributed to this
                model, excluding native tool failures after execution starts.
            **kwargs: SDK arguments, with request_max_retries fixed at 1.

        Returns:
            The original SDK response.

        Raises:
            ProviderUnavailableError: Cooldown or another probe prevents a call.
            Exception: Original SDK error or an empty manual test response.
        """
        provider_id = chat_provider_id
        state = self._states.setdefault(provider_id, _HealthState())
        instances = getattr(
            getattr(self.context, "provider_manager", None), "inst_map", None
        )
        if isinstance(instances, dict):
            provider = instances.get(provider_id)
            configuration = getattr(provider, "provider_config", {})
            signature = hashlib.sha256(
                f"{id(provider)}:{json.dumps(configuration, sort_keys=True, default=str)}".encode()
            ).hexdigest()
            if state.signature not in (None, signature):
                state = self._states[provider_id] = _HealthState(signature=signature)
            state.signature = signature

        if state.probing or (not force_probe and state.retry_at):
            raise ProviderUnavailableError(
                f"Provider {provider_id} is temporarily unavailable"
            )
        probe = force_probe
        if probe:
            state.generation += 1
            state.probing = True
        generation = state.generation
        state.last_attempt = time.time()
        started = self.clock()
        try:
            # Giftia owns retries; avoid stacking AstrBot's default attempts.
            kwargs["request_max_retries"] = 1
            pending = operation(chat_provider_id=provider_id, **kwargs)
            response = (
                await asyncio.wait_for(pending, request_timeout)
                if request_timeout is not None
                else await pending
            )
            if (
                force_probe
                and not (getattr(response, "completion_text", "") or "").strip()
            ):
                raise EmptyModelOutputError("Connectivity test returned no text")
        except Exception as exc:
            if (
                self._states.get(provider_id) is state
                and state.generation == generation
                and (count_failure is None or count_failure())
            ):
                state.last_latency_ms = round((self.clock() - started) * 1000)
                failure = self._classify_error(exc)
                if failure is not None:
                    reason, delay, immediate = failure
                    state.failures += 1
                    state.last_failure = time.time()
                    state.reason = reason
                else:
                    delay, immediate = 0, False
                if (
                    failure is not None
                    and (probe or immediate or state.failures >= self.FAILURE_THRESHOLD)
                ) or (probe and state.retry_at):
                    # A failed probe, including empty output or a neutral error,
                    # must wait again rather than becoming a tight retry loop.
                    cooldown = self.COOLDOWNS[
                        min(state.backoff, len(self.COOLDOWNS) - 1)
                    ]
                    state.retry_at = self.clock() + max(cooldown, delay)
                    state.backoff += 1
                    state.generation += 1
                    state.probing = False
                    logger.warning(
                        "Provider entered cooldown. provider_id: %s, reason: %s, seconds: %s",
                        provider_id,
                        state.reason,
                        max(cooldown, delay),
                    )
            raise
        else:
            if (
                self._states.get(provider_id) is state
                and state.generation == generation
            ):
                if state.retry_at:
                    logger.info("Provider recovered. provider_id: %s", provider_id)
                state.failures = 0
                state.retry_at = 0
                state.backoff = 0
                state.reason = ""
                state.last_success = time.time()
                state.last_latency_ms = round((self.clock() - started) * 1000)
            return response
        finally:
            if (
                self._states.get(provider_id) is state
                and state.generation == generation
            ):
                state.last_latency_ms = round((self.clock() - started) * 1000)
                state.probing = False

    def snapshot(self, provider_id: str) -> dict:
        """Describe a provider without probing or exposing request credentials.

        Args:
            provider_id: AstrBot provider ID.

        Returns:
            Public status, cooldown, recent timestamps and a classified reason.
        """
        state = self._states.get(provider_id) or _HealthState()
        remaining = max(0, state.retry_at - self.clock())
        status = "healthy" if state.last_success else "unknown"
        if state.failures:
            status = "degraded"
        if state.retry_at:
            status = "cooldown" if remaining else "probe_due"
        if state.probing:
            status = "probing"
        return {
            "provider_id": provider_id,
            "status": status,
            "failures": state.failures,
            "retry_in_seconds": math.ceil(remaining),
            "last_attempt": state.last_attempt,
            "last_success": state.last_success,
            "last_failure": state.last_failure,
            "last_latency_ms": state.last_latency_ms,
            "reason": state.reason,
        }

    @staticmethod
    def _classify_error(error: Exception) -> tuple[str, float, bool] | None:
        """Recognize service failures, including wrapped provider SDK errors.

        Args:
            error: Exception raised at the model request boundary.

        Returns:
            A safe reason, minimum cooldown and immediate-trip flag, or None for
            input, safety, output, tool and unrecognized local errors.
        """
        if isinstance(error, EmptyModelOutputError):
            return None
        seen = set()
        current: BaseException | None = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            message = str(current).lower()
            if any(
                term in message
                for term in (
                    "safety check",
                    "platform policy",
                    "content_policy_violation",
                    "content_filter",
                )
            ):
                return None
            # HTTP resource errors may originate from downloading media before
            # inference. Provider SDK status errors carry their own status_code.
            if type(current).__name__ == "HTTPStatusError" and type(
                current
            ).__module__.startswith("httpx"):
                return None
            response = getattr(current, "response", None)
            code = getattr(current, "status_code", None) or getattr(
                current, "code", None
            )
            if isinstance(code, str) and code.isdigit():
                code = int(code)
            if not isinstance(code, int):
                code = getattr(response, "status_code", None)
            body = getattr(current, "body", None) or getattr(current, "details", None)
            details = body.get("error", body) if isinstance(body, dict) else {}
            error_code = str(
                details.get("code", "") if isinstance(details, dict) else ""
            ).lower()
            delay = 0.0
            headers = (
                getattr(response, "headers", None)
                or getattr(current, "headers", None)
                or {}
            )
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(0, float(retry_after))
                except (ValueError, TypeError):
                    try:
                        deadline = parsedate_to_datetime(str(retry_after))
                        if deadline.tzinfo is None:
                            deadline = deadline.replace(tzinfo=timezone.utc)
                        delay = max(0, deadline.timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
            daily_quota = False
            if isinstance(details, dict):
                for detail in details.get("details", []) or []:
                    if not isinstance(detail, dict):
                        continue
                    if str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
                        try:
                            delay = max(
                                delay,
                                float(
                                    str(detail.get("retryDelay", "0")).removesuffix("s")
                                ),
                            )
                        except ValueError:
                            pass
                    for violation in detail.get("violations", []) or []:
                        if isinstance(violation, dict):
                            quota_id = str(violation.get("quotaId", "")).lower()
                            daily_quota |= "perday" in quota_id or "per_day" in quota_id
            if not math.isfinite(delay):
                delay = 0
            if (
                error_code
                in {
                    "insufficient_quota",
                    "credit_balance_exhausted",
                    "organization_spend_limit_exceeded",
                    "project_spend_limit_exceeded",
                    "organization_usage_limit_exceeded",
                }
                or daily_quota
            ):
                return "quota", max(600, delay), True
            if code in (401, 403):
                return "authentication", max(600, delay), True
            if code == 404 or type(current).__name__ == "ProviderNotFoundError":
                return "provider_missing", max(600, delay), True
            if code == 429:
                return "rate_limit", delay, True
            if code == 408:
                return "connection", delay, bool(delay)
            if isinstance(code, int) and 500 <= code <= 599:
                return "server_error", delay, bool(delay)
            if any(
                term in message
                for term in ("api key not valid", "invalid api key", "invalid_api_key")
            ):
                return "authentication", 600, True
            if isinstance(current, (TimeoutError, ConnectionError)) or type(
                current
            ).__name__ in {
                "APIConnectionError",
                "APITimeoutError",
                "ConnectError",
                "ConnectTimeout",
                "ReadTimeout",
                "WriteTimeout",
                "PoolTimeout",
                "ClientConnectorError",
                "ServerDisconnectedError",
                "ServerTimeoutError",
                "ClientConnectionError",
                "ClientOSError",
                "ReadError",
                "WriteError",
                "RemoteProtocolError",
            }:
                return "connection", 0, False
            current = current.__cause__ or current.__context__
        return None


def get_provider_health(plugin, context=None) -> ProviderHealth:
    """Get the single health tracker shared by every model path in a plugin.

    Args:
        plugin: Owning plugin, or None for a standalone model caller.
        context: SDK context when constructing a standalone caller.

    Returns:
        The shared tracker, lazily initialized for independently created services.
    """
    health = getattr(plugin, "provider_health", None)
    if not isinstance(health, ProviderHealth):
        health = ProviderHealth(context if context is not None else plugin.context)
        if plugin is not None:
            plugin.provider_health = health
    return health
