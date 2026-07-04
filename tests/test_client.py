"""Tests for HueClient TLS configuration and response parsing (no network).

The pinning pre-flight is monkeypatched so these never touch a bridge; the focus
is that the *live session* actually enforces the pinned fingerprint on every
connection rather than running unverified.
"""

from __future__ import annotations

import pytest

from hueman import client as client_mod
from hueman.client import HueClient
from hueman.config import Bridge, TlsConfig

FP = "9142" + "0" * 60  # 64 hex chars


def _bridge(mode: str, **tls_kw) -> Bridge:
    return Bridge(
        host="bridge.local",
        application_key="abc123",
        tls=TlsConfig(mode=mode, **tls_kw),
    )


def test_pin_mode_enforces_fingerprint_on_live_session(monkeypatch):
    monkeypatch.setattr(client_mod, "verify_or_pin", lambda host, pin_file: FP)
    c = HueClient(_bridge("pin"))

    adapter = c._session.get_adapter("https://bridge.local")
    assert isinstance(adapter, client_mod._FingerprintAdapter)
    # urllib3 must be told to assert the pinned leaf-cert fingerprint on every
    # connection this session opens (requests + the reused SSE stream).
    kw = adapter.poolmanager.connection_pool_kw
    assert kw.get("assert_fingerprint") == FP
    assert kw.get("cert_reqs") == "CERT_NONE"


def test_insecure_mode_has_no_fingerprint_adapter(monkeypatch):
    c = HueClient(_bridge("insecure"))
    adapter = c._session.get_adapter("https://bridge.local")
    assert not isinstance(adapter, client_mod._FingerprintAdapter)
    assert c._session.verify is False


def test_cacert_mode_uses_ca_bundle(monkeypatch):
    c = HueClient(_bridge("cacert", cacert="/etc/ssl/bridge-ca.pem"))
    adapter = c._session.get_adapter("https://bridge.local")
    assert not isinstance(adapter, client_mod._FingerprintAdapter)
    assert c._session.verify == "/etc/ssl/bridge-ca.pem"


def test_create_application_key_non_json_response_is_clean_error(monkeypatch):
    """A captive portal / proxy returning HTML must surface as a HueIacError the
    CLI can print, not a raw requests JSONDecodeError traceback."""
    import requests

    from hueman.errors import HueIacError

    monkeypatch.setattr(client_mod, "verify_or_pin", lambda host, pin_file: FP)
    c = HueClient(_bridge("pin"))

    class _HtmlResp:
        status_code = 200

        def json(self):
            raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)

    monkeypatch.setattr(c._session, "post", lambda *a, **k: _HtmlResp())
    with pytest.raises(HueIacError):
        c.create_application_key()
