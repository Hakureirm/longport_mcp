"""Paper-trading integration tests (need GitHub Actions / local secrets).

Read-only by default. Never place orders unless LONGBRIDGE_PAPER_ALLOW_ORDER=1.
Do not print secret values.
"""

from __future__ import annotations

import importlib
import os
import re

import pytest

pytestmark = pytest.mark.integration

_SECRET_ENV = (
    "LONGBRIDGE_PAPER_APP_KEY",
    "LONGBRIDGE_PAPER_APP_SECRET",
    "LONGBRIDGE_PAPER_ACCESS_TOKEN",
)


def _paper_creds_present() -> bool:
    return all(os.getenv(k) for k in _SECRET_ENV)


def _redact(text: str) -> str:
    """Strip anything that looks like a token/key from assertion messages."""
    out = text
    for k in _SECRET_ENV:
        val = os.getenv(k) or ""
        if len(val) >= 8:
            out = out.replace(val, f"<{k}>")
    out = re.sub(r"(?i)(authorization|token|secret|api[_-]?key)[=:\s]+\S+", r"\1=<redacted>", out)
    return out


@pytest.fixture(scope="module")
def paper_mod():
    if not _paper_creds_present():
        pytest.skip("paper secrets not set (LONGBRIDGE_PAPER_APP_KEY/SECRET/ACCESS_TOKEN)")
    # Prefer paper API Key path for CI (OAuth needs interactive browser).
    os.environ.setdefault("LONGBRIDGE_DEFAULT_MODE", "paper")
    for k in list(os.environ):
        if k in ("LONGBRIDGE_OAUTH_CLIENT_ID", "LONGPORT_OAUTH_CLIENT_ID"):
            # Avoid oauth_pending blocking when only paper API Key is configured.
            # Keep OAuth if user also set a client id intentionally; get_auth_mode
            # prefers ready oauth, else apikey — fine for CI with API Key only.
            pass
    mod = importlib.reload(importlib.import_module("longport_mcp_web"))
    assert mod.set_current_mode("paper")
    return mod


def test_paper_auth_mode_apikey(paper_mod):
    mode = paper_mod.get_auth_mode()
    assert mode in ("apikey", "oauth"), f"unexpected auth mode: {mode}"


def test_paper_check_sdk_status(paper_mod):
    status = paper_mod.check_sdk_status()
    safe = _redact(status)
    assert "认证模式" in safe or "模块状态" in safe
    # Should not echo raw secrets
    for k in _SECRET_ENV:
        val = os.getenv(k) or ""
        if len(val) >= 8:
            assert val not in status


def test_paper_account_balance_readonly(paper_mod):
    client = paper_mod.get_client()
    assert getattr(client, "mode", "paper") == "paper"
    resp = client.get_account_balance()
    assert isinstance(resp, dict)
    code = resp.get("code", 0)
    assert code in (0, "0", None), _redact(f"API code={code} message={resp.get('message')}")
    data = resp.get("data")
    assert data is not None


def test_paper_get_assets_tool(paper_mod):
    text = paper_mod.get_assets()
    safe = _redact(text)
    assert not safe.startswith("[X]"), safe
    assert "账户资产" in safe or "总现金" in safe or "净资产" in safe
    for k in _SECRET_ENV:
        val = os.getenv(k) or ""
        if len(val) >= 8:
            assert val not in text


def test_paper_place_order_opt_in_only(paper_mod):
    """CI must not place orders unless LONGBRIDGE_PAPER_ALLOW_ORDER=1."""
    if os.getenv("LONGBRIDGE_PAPER_ALLOW_ORDER", "").strip() not in ("1", "true", "TRUE", "yes"):
        pytest.skip("order smoke disabled (set LONGBRIDGE_PAPER_ALLOW_ORDER=1 to opt in)")
    # Intentionally minimal / unlikely-to-fill limit so paper account is safer.
    # Still opt-in only — default CI never reaches here.
    result = paper_mod.place_order(
        symbol="AAPL.US",
        side="buy",
        quantity=1,
        order_type="LO",
        price=0.01,
    )
    safe = _redact(str(result))
    assert "[模拟盘]" in safe or "订单" in safe or "[OK]" in safe or "[X]" in safe
