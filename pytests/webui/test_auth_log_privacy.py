"""Synthetic credentials only; exercise real authentication without a server."""

from types import SimpleNamespace
from unittest.mock import Mock
import logging

from fastapi import HTTPException, Request, Response
import pytest

from src.webui import app, routes
from src.webui.core import auth, security
from src.webui.routers.websocket import auth as ws


TOKENS = ["Aa!2345678", "Middle!Aa9876543210xx", "Long!Aa" + "9876543210" * 12]


@pytest.fixture
def manager(tmp_path, monkeypatch):
    value = security.TokenManager(tmp_path / "synthetic-auth.json")
    for module in (auth, routes, ws):
        monkeypatch.setattr(module, "get_token_manager", lambda: value)
    monkeypatch.setattr("src.webui.core.get_token_manager", lambda: value)
    return value


def assert_private(caplog, *tokens):
    # Inspect rendered messages AND raw arguments/traceback records.
    captured = caplog.text + repr([(r.msg, r.args, r.exc_info) for r in caplog.records])
    for token in tokens:
        assert token not in captured
        assert token[:8] not in captured
        assert token[-8:] not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize("token", TOKENS)
@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.CRITICAL])
async def test_login_cookie_and_failure_privacy(manager, monkeypatch, caplog, token, level):
    caplog.set_level(level)
    assert manager._validate_custom_token(token)[0]
    assert manager.update_token(token)[0]
    manager._ensure_config()
    limiter = SimpleNamespace(reset_failures=Mock(), record_failed_attempt=Mock(return_value=(False, 4)))
    monkeypatch.setattr(routes, "get_rate_limiter", lambda: limiter)
    request = Request({"type": "http", "scheme": "https", "path": "/", "headers": [], "server": ("test", 443)})
    response = Response()
    result = await routes.verify_token(routes.TokenVerifyRequest(token=token), request, response, None)
    assert result.valid
    cookie = response.headers["set-cookie"]
    assert token in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert auth.get_current_token(token) == token
    assert auth.verify_auth_token_from_cookie_or_header(token)
    invalid = "Invalid!Aa99999999"
    response = Response()
    result = await routes.verify_token(routes.TokenVerifyRequest(token=invalid), request, response, None)
    assert not result.valid and "set-cookie" not in response.headers
    limiter.record_failed_attempt.assert_called_once()
    with pytest.raises(HTTPException) as exc:
        auth.get_current_token(invalid)
    assert exc.value.status_code == 401
    assert_private(caplog, token, invalid)


def test_generation_regeneration_startup_and_ws(manager, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    manager.update_token(TOKENS[0])
    generated = manager.regenerate_token()
    assert manager.verify_token(generated) and not manager.verify_token(TOKENS[0])
    temporary = manager._create_new_token()
    app.show_access_token()
    assert manager.get_token() == temporary
    ws._ws_temp_tokens.clear()
    value = ws.generate_ws_token(temporary)
    assert ws.verify_ws_token(value)
    assert not ws.verify_ws_token(value)
    value2 = ws.generate_ws_token(temporary)
    manager.update_token(TOKENS[1])
    assert not ws.verify_ws_token(value2)
    assert_private(caplog, *TOKENS[:2], generated, temporary, value, value2)


@pytest.mark.asyncio
async def test_exception_messages_and_proxy_headers_are_not_logged(manager, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    secret = TOKENS[2]
    request = Request({"type": "http", "scheme": "http", "headers": [(b"x-forwarded-proto", secret.encode())], "server": ("test", 80)})
    auth.set_auth_cookie(Response(), secret, request)
    monkeypatch.setattr(manager, "verify_token", Mock(side_effect=RuntimeError(secret)))
    with pytest.raises(HTTPException) as exc:
        await routes.verify_token(routes.TokenVerifyRequest(token=secret), request, Response(), None)
    assert exc.value.status_code == 500
    monkeypatch.setattr(manager, "_save_config", Mock(side_effect=ValueError(secret)))
    assert not manager.update_token(secret)[0]
    assert "RuntimeError" in caplog.text and "ValueError" in caplog.text
    assert_private(caplog, secret)


def test_invalid_config_content_is_not_logged(manager, caplog):
    caplog.set_level(logging.DEBUG)
    manager.config_path.write_text('{"access_token": "' + TOKENS[0], encoding="utf-8")
    assert manager._load_config() == {}
    assert_private(caplog, TOKENS[0])
