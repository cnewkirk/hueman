"""Trust-on-first-use certificate pinning for the Hue bridge.

The bridge serves a self-signed certificate, so plain CA verification fails and
the lazy workaround everyone reaches for is ``verify=False`` — which silently
accepts any MITM on your LAN. Instead we pin the leaf certificate's SHA-256
fingerprint: captured on first connect (TOFU), stored in a small JSON file, and
checked on every subsequent connect. This gives real identity verification
without shipping Signify's CA bundle.

This module only captures/loads the pinned fingerprint (a raw TLS handshake at
startup, TOFU on first run). Enforcement is NOT a one-shot pre-flight check —
that alone would leave a TOCTOU window between the probe and the real requests.
Instead :class:`hue_iac.client._FingerprintAdapter` passes the fingerprint to
urllib3 as ``assert_fingerprint``, so *every* connection the session opens
(including the long-lived SSE stream) verifies the leaf certificate against the
pin at handshake time.
"""

from __future__ import annotations

import hashlib
import json
import socket
import ssl
from pathlib import Path

from .errors import PinError


def fetch_fingerprint(host: str, port: int = 443, timeout: float = 5.0) -> str:
    """Return the lowercase hex SHA-256 of the server's leaf certificate."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                der = ssock.getpeercert(binary_form=True)
    except OSError as e:
        raise PinError(f"could not reach bridge at {host}:{port} ({e})")
    if not der:
        raise PinError(f"bridge at {host} presented no certificate")
    return hashlib.sha256(der).hexdigest()


def _load_store(pin_file: Path) -> dict:
    """Return the parsed pin store.

    Distinguishes "file absent" (legitimate first run -> ``{}``) from "file
    present but unreadable/corrupt" (fail closed with :class:`PinError`) so a
    truncated or tampered store is never silently re-pinned, which would defeat
    the certificate-change alert this module exists to provide.
    """
    if not pin_file.is_file():
        return {}
    try:
        raw = pin_file.read_text()
    except OSError as e:
        raise PinError(f"could not read pin file {pin_file} ({e}); refusing to re-pin")
    try:
        store = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PinError(
            f"pin file {pin_file} is corrupt ({e}); delete it to deliberately re-pin"
        )
    if not isinstance(store, dict):
        raise PinError(f"pin file {pin_file} is corrupt (expected an object); delete it to re-pin")
    return store


def verify_or_pin(host: str, pin_file: str | Path, *, allow_new: bool = True) -> str:
    """Verify ``host`` against the stored pin, recording it on first sight.

    Returns the confirmed fingerprint. Raises :class:`PinError` if the
    certificate changed (possible MITM or a genuine bridge swap — in which case
    the user deletes the pin file to re-pin deliberately).
    """
    pin_file = Path(pin_file)
    store = _load_store(pin_file)
    current = fetch_fingerprint(host)
    known = store.get(host)

    if known is None:
        if not allow_new:
            raise PinError(f"no stored pin for {host} and pinning of new hosts is disabled")
        store[host] = current
        pin_file.write_text(json.dumps(store, indent=2, sort_keys=True))
        return current

    if known.lower() != current.lower():
        raise PinError(
            f"certificate fingerprint for {host} changed!\n"
            f"  pinned:  {known}\n"
            f"  current: {current}\n"
            f"If you intentionally replaced the bridge, delete {pin_file} and re-run."
        )
    return current
