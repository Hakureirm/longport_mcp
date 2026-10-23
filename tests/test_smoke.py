"""Smoke tests that do not require Longbridge credentials."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _clear_longbridge_env():
    for key in list(os.environ):
        if key.startswith(("LONGPORT_", "LONGBRIDGE_")):
            os.environ.pop(key, None)


def test_import_module():
    mod = importlib.import_module("longport_mcp_web")
    assert hasattr(mod, "mcp")
    assert hasattr(mod, "main")
    assert hasattr(mod, "HAS_SDK")


def test_tools_register_without_credentials():
    _clear_longbridge_env()
    mod = importlib.reload(importlib.import_module("longport_mcp_web"))
    mcp = mod.mcp

    tools = None
    if hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
        tools = mcp._tool_manager._tools
    elif hasattr(mcp, "_tools"):
        tools = mcp._tools

    assert tools is not None, "Unable to locate registered MCP tools on FastMCP instance"
    names = set(tools.keys()) if isinstance(tools, dict) else {getattr(t, "name", str(t)) for t in tools}

    expected = {
        "place_order",
        "get_quote",
        "get_candlesticks",
        "switch_account_mode",
        "check_sdk_status",
        "get_market_status",
        "get_company_info",
        "get_finance_calendar",
        "oauth_login",
        "oauth_status",
    }
    missing = expected - names
    assert not missing, f"Missing tools: {missing}"
    assert len(names) >= 40


def test_resolve_sdk_period_month_vs_minute():
    pytest.importorskip("longbridge")
    mod = importlib.import_module("longport_mcp_web")
    if not mod.HAS_SDK:
        pytest.skip("longbridge SDK not installed")
    assert mod._resolve_sdk_period("1M") == mod.Period.Month
    assert mod._resolve_sdk_period("1m") == mod.Period.Min_1
    assert mod._resolve_sdk_period("day") == mod.Period.Day


def test_env_helper_prefers_longbridge_prefix(monkeypatch):
    mod = importlib.import_module("longport_mcp_web")
    monkeypatch.setenv("LONGPORT_APP_KEY", "old")
    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "new")
    assert mod._env("LONGBRIDGE_APP_KEY", "LONGPORT_APP_KEY") == "new"


def test_paper_header_on_rest_client(monkeypatch):
    mod = importlib.import_module("longport_mcp_web")
    monkeypatch.setenv("LONGBRIDGE_PAPER_APP_KEY", "k")
    monkeypatch.setenv("LONGBRIDGE_PAPER_APP_SECRET", "s")
    monkeypatch.setenv("LONGBRIDGE_PAPER_ACCESS_TOKEN", "t")
    client = mod.LongportClient(mode="paper")
    headers = client._build_headers("GET", "/v1/asset/account")
    assert headers.get("x-papertrading") == "true"


def test_auth_mode_none_without_credentials(monkeypatch):
    _clear_longbridge_env()
    mod = importlib.reload(importlib.import_module("longport_mcp_web"))
    monkeypatch.setattr(mod, "_oauth_handle", None)
    assert mod.get_auth_mode() == "none"


def test_auth_mode_oauth_pending_with_client_id(monkeypatch):
    _clear_longbridge_env()
    monkeypatch.setenv("LONGBRIDGE_OAUTH_CLIENT_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    mod = importlib.reload(importlib.import_module("longport_mcp_web"))
    monkeypatch.setattr(mod, "_oauth_handle", None)
    monkeypatch.setattr(mod, "_oauth_token_exists", lambda client_id=None: False)
    assert mod.get_auth_mode() == "oauth_pending"
    status = mod.oauth_status()
    assert "oauth_pending" in status
    assert "oauth_login" in status


def test_auth_mode_apikey_fallback(monkeypatch):
    _clear_longbridge_env()
    monkeypatch.setenv("LONGBRIDGE_APP_KEY", "k")
    monkeypatch.setenv("LONGBRIDGE_APP_SECRET", "s")
    monkeypatch.setenv("LONGBRIDGE_ACCESS_TOKEN", "t")
    mod = importlib.reload(importlib.import_module("longport_mcp_web"))
    monkeypatch.setattr(mod, "_oauth_handle", None)
    monkeypatch.setattr(mod, "_oauth_token_exists", lambda client_id=None: False)
    assert mod.get_auth_mode() == "apikey"


def test_auth_mode_oauth_when_token_on_disk(monkeypatch, tmp_path):
    _clear_longbridge_env()
    cid = "oauth-client-smoke-test"
    monkeypatch.setenv("LONGBRIDGE_OAUTH_CLIENT_ID", cid)
    mod = importlib.reload(importlib.import_module("longport_mcp_web"))
    token_dir = tmp_path / "tokens" / cid
    token_dir.mkdir(parents=True)
    (token_dir / "access.json").write_text('{"access_token":"fake"}')
    monkeypatch.setattr(mod, "_oauth_token_path", lambda client_id=None: token_dir)
    monkeypatch.setattr(mod, "_oauth_handle", None)
    # _oauth_token_exists uses path — rebind to use tmp_path
    def _exists(client_id=None):
        p = token_dir
        return p.exists() and any(p.iterdir())

    monkeypatch.setattr(mod, "_oauth_token_exists", _exists)
    assert mod.get_auth_mode() == "oauth"


def test_oauth_login_without_client_id(monkeypatch):
    _clear_longbridge_env()
    mod = importlib.reload(importlib.import_module("longport_mcp_web"))
    msg = mod.oauth_login()
    assert "未配置" in msg or "client_id" in msg.lower() or "LONGBRIDGE_OAUTH_CLIENT_ID" in msg


def test_build_sdk_config_prefers_oauth(monkeypatch):
    pytest.importorskip("longbridge")
    mod = importlib.import_module("longport_mcp_web")
    if not mod.HAS_SDK:
        pytest.skip("longbridge SDK not installed")

    fake_oauth = object()
    captured = {}

    def fake_from_oauth(oauth, **kwargs):
        captured["oauth"] = oauth
        captured["kwargs"] = kwargs
        return MagicMock(name="Config")

    monkeypatch.setattr(mod, "_ensure_oauth", lambda interactive=False: fake_oauth)
    monkeypatch.setattr(mod.Config, "from_oauth", staticmethod(fake_from_oauth))
    cfg = mod._build_sdk_config("paper")
    assert cfg is not None
    assert captured["oauth"] is fake_oauth
    assert captured["kwargs"].get("enable_papertrading") is True


def test_oauth_rest_client_shares_business_methods():
    mod = importlib.import_module("longport_mcp_web")
    for name in ("get_account_balance", "submit_order", "get_quote", "estimate_buy_limit"):
        assert hasattr(mod.OAuthRestClient, name), f"OAuthRestClient missing {name}"
