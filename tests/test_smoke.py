"""Smoke tests that do not require Longbridge credentials."""

from __future__ import annotations

import importlib
import os

import pytest


def test_import_module():
    mod = importlib.import_module("longport_mcp_web")
    assert hasattr(mod, "mcp")
    assert hasattr(mod, "main")
    assert hasattr(mod, "HAS_SDK")


def test_tools_register_without_credentials():
    # Ensure no credentials are present so client init stays lazy/fails only on use
    for key in list(os.environ):
        if key.startswith(("LONGPORT_", "LONGBRIDGE_")):
            os.environ.pop(key, None)

    mod = importlib.reload(importlib.import_module("longport_mcp_web"))
    mcp = mod.mcp

    # FastMCP stores tools on _tool_manager._tools in recent versions
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
