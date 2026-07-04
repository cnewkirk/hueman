"""Tests for trust-on-first-use certificate pinning (no network).

``fetch_fingerprint`` is monkeypatched throughout so these exercise the
store/compare/persist logic without touching a real bridge.
"""

from __future__ import annotations

import json

import pytest

from hue_iac import pin
from hue_iac.errors import PinError

FP_A = "a" * 64
FP_B = "b" * 64


@pytest.fixture
def pin_file(tmp_path):
    return tmp_path / ".hue-pin.json"


def _stub_fp(monkeypatch, value: str) -> None:
    monkeypatch.setattr(pin, "fetch_fingerprint", lambda *a, **k: value)


def test_first_pin_records_and_returns_fingerprint(monkeypatch, pin_file):
    _stub_fp(monkeypatch, FP_A)
    result = pin.verify_or_pin("bridge.local", pin_file)
    assert result == FP_A
    assert json.loads(pin_file.read_text())["bridge.local"] == FP_A


def test_matching_pin_returns_fingerprint(monkeypatch, pin_file):
    pin_file.write_text(json.dumps({"bridge.local": FP_A}))
    _stub_fp(monkeypatch, FP_A)
    assert pin.verify_or_pin("bridge.local", pin_file) == FP_A


def test_changed_fingerprint_raises(monkeypatch, pin_file):
    pin_file.write_text(json.dumps({"bridge.local": FP_A}))
    _stub_fp(monkeypatch, FP_B)
    with pytest.raises(PinError, match="changed"):
        pin.verify_or_pin("bridge.local", pin_file)


def test_new_host_with_allow_new_false_raises(monkeypatch, pin_file):
    _stub_fp(monkeypatch, FP_A)
    with pytest.raises(PinError, match="no stored pin"):
        pin.verify_or_pin("bridge.local", pin_file, allow_new=False)


def test_corrupt_pin_file_fails_closed_and_does_not_repin(monkeypatch, pin_file):
    """A present-but-unparseable store must NOT be silently re-pinned."""
    pin_file.write_text("{ this is not valid json")
    _stub_fp(monkeypatch, FP_B)
    with pytest.raises(PinError, match="could not read|corrupt|unreadable"):
        pin.verify_or_pin("bridge.local", pin_file)
    # The bogus content must be left untouched, not overwritten with a new pin.
    assert pin_file.read_text() == "{ this is not valid json"
