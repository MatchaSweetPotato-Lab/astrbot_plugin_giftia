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

    def __init__(self, context, *, clock: Callable[[], float] = time.monotonic):
        self.context = context
        self.clock = clock
        self._states: dict[str, _HealthState] = {}

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
        """Run one SDK call without changing the caller's retry policy.

        State transitions do not await, so admission and probe ownership are
        atomic on the plugin event loop. Generations ignore results from requests
        admitted before a cooldown, configuration change or manual test.

        Args:
            operation: AstrBot generation or native tool-loop method.
            chat_provider_id: Shared AstrBot provider configuration ID.
            request_timeout: Optional timeout around the actual model operation.
            force_probe: Bypass cooldown for a manual text connectivity test.
            count_failure: Whether an exception can still be attributed to this
                model, excluding native tool failures after execution starts.
            **kwargs: Arguments passed unchanged to the SDK method.

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

        now = self.clock()
        if state.probing or (not force_probe and state.retry_at > now):
            raise ProviderUnavailableError(
                f"Provider {provider_id} is temporarily unavailable"
            )
        probe = force_probe or bool(state.retry_at)
        if probe:
            state.generation += 1
            state.probing = True
        generation = state.generation
        state.last_attempt = time.time()
        started = self.clock()
        try:
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
                and (failure := self._classify_error(exc)) is not None
            ):
                reason, delay, immediate = failure
                state.failures += 1
                state.last_failure = time.time()
                state.reason = reason
                state.last_latency_ms = round((self.clock() - started) * 1000)
                if probe or immediate or state.failures >= self.FAILURE_THRESHOLD:
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
                        reason,
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
