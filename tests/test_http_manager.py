import ssl
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientConnectorCertificateError, ClientConnectorSSLError
from core.utils import http_manager


@pytest.fixture
def manager(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(http_manager, "ClientSession", MagicMock(return_value=session))
    return http_manager.HttpManager({})


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", ["certificate", "handshake"])
async def test_ssl_failure_rejects_media_without_retry(
    manager, monkeypatch, error_type
):
    connection = SimpleNamespace(host="media.example", port=443, ssl=True, is_ssl=True)
    if error_type == "certificate":
        error = ClientConnectorCertificateError(
            connection, ssl.SSLCertVerificationError("certificate verify failed")
        )
    else:
        error = ClientConnectorSSLError(connection, ssl.SSLError("handshake failed"))
    failed_request = MagicMock()
    failed_request.__aenter__ = AsyncMock(side_effect=error)
    untrusted_response = MagicMock(status=200)
    untrusted_response.read = AsyncMock(return_value=b"untrusted media")
    fallback_request = MagicMock()
    fallback_request.__aenter__ = AsyncMock(return_value=untrusted_response)
    manager.session.get.side_effect = [failed_request, fallback_request]
    warning = MagicMock()
    monkeypatch.setattr(http_manager.logger, "warning", warning)

    result = await manager.download_media("https://media.example/image.png")

    assert result == b""
    manager.session.get.assert_called_once()
    untrusted_response.read.assert_not_awaited()
    warning.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("transient_failure", [False, True])
async def test_verified_download_succeeds_and_retries_transient_errors(
    manager, transient_failure
):
    response = MagicMock(status=200)
    response.read = AsyncMock(return_value=b"media bytes")
    request = MagicMock()
    request.__aenter__ = AsyncMock(return_value=response)
    attempts = [request]
    if transient_failure:
        failed_request = MagicMock()
        failed_request.__aenter__ = AsyncMock(side_effect=TimeoutError())
        attempts.insert(0, failed_request)
    manager.session.get.side_effect = attempts

    result = await manager.download_media("https://media.example/image.png")

    assert result == b"media bytes"
    assert manager.session.get.call_count == len(attempts)
    for call in manager.session.get.call_args_list:
        assert call.kwargs.get("ssl", True) is True
