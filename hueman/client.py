"""Thin client for the Philips Hue CLIP API v2 (local, HTTPS).

Covers exactly what the IaC workflow needs: link-button key creation, typed
resource GET/POST/PUT/DELETE, and a couple of convenience lookups. TLS identity
is established up-front via :mod:`hueman.pin` (trust-on-first-use) or a CA
bundle; we never blindly disable verification without having pinned first.

API surface used here is the documented v2 model
(https://developers.meethue.com/develop/hue-api-v2/): resources live at
``/clip/v2/resource/<type>`` and are addressed by stable ``rid`` UUIDs, with the
application key passed in the ``hue-application-key`` header.
"""

from __future__ import annotations

from typing import Any, cast

import urllib3
import requests
from requests.adapters import HTTPAdapter

from .config import Bridge, TlsConfig
from .errors import AuthError, BridgeError
from .pin import verify_or_pin

_KEY_CREATE_DEVICETYPE = "hueman#cli"


class _FingerprintAdapter(HTTPAdapter):
    """Pins the bridge's leaf-cert SHA-256 on every connection the session opens.

    The Hue bridge serves a self-signed certificate, so CA verification cannot
    apply. Instead of disabling verification outright (which accepts any MITM),
    we hand urllib3 ``assert_fingerprint``: after each TLS handshake it checks
    the peer cert hash against the pinned value. Unlike a one-shot pre-flight
    probe, this enforces bridge identity on the *actual* request connections and
    on the long-lived SSE stream that reuses this session.
    """

    def __init__(self, fingerprint: str, **kwargs: Any) -> None:
        """Store the expected SHA-256 ``fingerprint`` and initialise the adapter."""
        self._fingerprint = fingerprint
        super().__init__(**kwargs)

    def _pin_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Inject the fingerprint-pinning TLS options into urllib3 pool kwargs."""
        kwargs["assert_fingerprint"] = self._fingerprint
        kwargs["cert_reqs"] = "CERT_NONE"  # self-signed: no CA chain to check
        kwargs["assert_hostname"] = False  # identity comes from the fingerprint
        return kwargs

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        """Create the connection pool with fingerprint pinning enforced."""
        super().init_poolmanager(
            connections, maxsize, block=block, **self._pin_kwargs(pool_kwargs)
        )

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        """Return a proxy manager that also enforces the pinned fingerprint."""
        return super().proxy_manager_for(proxy, **self._pin_kwargs(proxy_kwargs))


class HueClient:
    """Authenticated session against one Hue bridge's CLIP v2 API.

    Owns the ``requests.Session`` (TLS trust configured per the bridge's
    ``tls`` mode at construction) and exposes the typed-resource CRUD helpers
    the reconcilers and runtime build on. All errors surface as
    :class:`~hueman.errors.BridgeError` / :class:`~hueman.errors.AuthError`
    so callers never handle raw ``requests`` exceptions.

    Args:
        bridge: Bridge connection settings (host, application key, TLS mode).
        timeout: Per-request timeout in seconds.
    """

    def __init__(self, bridge: Bridge, *, timeout: float = 10.0) -> None:
        """Build the session, configure TLS, and attach the application key."""
        self.bridge = bridge
        self.timeout = timeout
        self._base = f"https://{bridge.host}"
        self._session = requests.Session()
        self._configure_tls(bridge.tls)
        if bridge.application_key:
            self._session.headers["hue-application-key"] = bridge.application_key

    # -- TLS ---------------------------------------------------------------- #
    def _configure_tls(self, tls: TlsConfig) -> None:
        """Apply the configured TLS trust mode (``cacert``/``pin``/``insecure``) to the session."""
        if tls.mode == "cacert":
            self._session.verify = tls.cacert
        elif tls.mode == "pin":
            # Discover/persist the expected fingerprint, then enforce it on every
            # real connection via the adapter (not just the pre-flight probe).
            fingerprint = verify_or_pin(self.bridge.host, tls.pin_file)
            self._session.verify = False
            self._session.mount(self._base, _FingerprintAdapter(fingerprint))
        else:  # insecure: explicit, user-waived — the one genuinely unverified mode
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self._session.verify = False

    # -- low level ---------------------------------------------------------- #
    def _url(self, path: str) -> str:
        """Return the absolute bridge URL for an API ``path``."""
        return f"{self._base}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        need_key: bool = True,
    ) -> dict[str, Any]:
        """Send one CLIP request and return the parsed JSON envelope.

        Args:
            method: HTTP verb (``GET``/``POST``/``PUT``/``DELETE``).
            path: API path starting with ``/`` (appended to the bridge base URL).
            json_body: JSON body to send, if any.
            need_key: Require a paired application key before sending (all
                endpoints except v1 key creation).

        Returns:
            The decoded response body (the v2 ``{"data": ..., "errors": ...}``
            envelope).

        Raises:
            AuthError: If ``need_key`` is set and no application key is configured.
            BridgeError: On transport failure, non-JSON response, HTTP >= 400,
                or per-call errors reported in the v2 ``errors`` array.
        """
        if need_key and not self.bridge.application_key:
            raise AuthError("no application key; run 'hueman auth' to pair with the bridge")
        try:
            resp = self._session.request(
                method, self._url(path), json=json_body, timeout=self.timeout
            )
        except requests.RequestException as e:
            raise BridgeError(f"{method} {path} failed: {e}")
        return self._parse(resp, method, path)

    @staticmethod
    def _parse(resp: requests.Response, method: str, path: str) -> dict[str, Any]:
        """Decode a CLIP response, surfacing v2 in-body errors as :class:`BridgeError`.

        The v2 API reports per-call problems in an ``errors`` array even on
        HTTP 200, so that array is checked before the status code.
        """
        try:
            body = resp.json()
        except ValueError:
            raise BridgeError(f"{method} {path}: non-JSON response (HTTP {resp.status_code})")
        # v2 reports per-call problems in an "errors" array even on HTTP 200.
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            msg = "; ".join(e.get("description", str(e)) for e in errors)
            raise BridgeError(f"{method} {path}: {msg}")
        if resp.status_code >= 400:
            raise BridgeError(f"{method} {path}: HTTP {resp.status_code}")
        # A successful v2 response is always a JSON object envelope.
        return cast("dict[str, Any]", body)

    # -- key creation (v1 endpoint, still the documented path) -------------- #
    def create_application_key(self) -> dict[str, Any]:
        """Pair with the bridge and return the new key material.

        Press the bridge link button first; the bridge only honours the
        request for ~30 seconds afterwards.

        Returns:
            The v1 ``success`` object, ``{"username": ..., "clientkey": ...}``.

        Raises:
            AuthError: If the link button was not pressed or the bridge
                rejected the request.
            BridgeError: On transport failure or a non-JSON response.
        """
        try:
            resp = self._session.post(
                self._url("/api"),
                json={"devicetype": _KEY_CREATE_DEVICETYPE, "generateclientkey": True},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise BridgeError(f"key creation failed: {e}")
        try:
            data = resp.json()
        except ValueError as e:  # JSONDecodeError; e.g. a captive portal's HTML
            raise BridgeError(f"key creation returned a non-JSON response: {e}")
        entry = data[0] if isinstance(data, list) and data else {}
        if "error" in entry:
            desc = entry["error"].get("description", "")
            if "link button" in desc.lower():
                raise AuthError("press the bridge link button, then re-run within 30s")
            raise AuthError(f"key creation rejected: {desc}")
        success = entry.get("success", {})
        if "username" not in success:
            raise AuthError(f"unexpected key-creation response: {data!r}")
        return cast("dict[str, Any]", success)

    # -- typed resource helpers -------------------------------------------- #
    def get_resources(self, rtype: str) -> list[dict[str, Any]]:
        """Return every resource of type ``rtype`` (empty list if none)."""
        data: list[dict[str, Any]] = self._request(
            "GET", f"/clip/v2/resource/{rtype}"
        ).get("data", [])
        return data

    def get_all_resources(self) -> list[dict[str, Any]]:
        """Return every resource of every type — the authoritative full census.

        Use this (not a hand-picked list of types) before concluding a resource
        kind is absent; new/unknown types (e.g. MotionAware) show up here.
        """
        data: list[dict[str, Any]] = self._request("GET", "/clip/v2/resource").get("data", [])
        return data

    def get_resource(self, rtype: str, rid: str) -> dict[str, Any] | None:
        """Return the resource ``rid`` of type ``rtype``, or ``None`` if absent."""
        data: list[dict[str, Any]] = self._request(
            "GET", f"/clip/v2/resource/{rtype}/{rid}"
        ).get("data", [])
        return data[0] if data else None

    def create_resource(self, rtype: str, body: dict[str, Any]) -> str:
        """Create a resource of type ``rtype`` and return its new ``rid``.

        Returns an empty string if the bridge acknowledged the create without
        echoing the new resource reference.
        """
        data: list[dict[str, Any]] = self._request(
            "POST", f"/clip/v2/resource/{rtype}", json_body=body
        ).get("data", [])
        if not data:
            return ""
        rid: str = data[0]["rid"]
        return rid

    def update_resource(self, rtype: str, rid: str, body: dict[str, Any]) -> None:
        """PUT ``body`` onto the resource ``rid`` of type ``rtype``."""
        self._request("PUT", f"/clip/v2/resource/{rtype}/{rid}", json_body=body)

    def delete_resource(self, rtype: str, rid: str) -> None:
        """Delete the resource ``rid`` of type ``rtype``."""
        self._request("DELETE", f"/clip/v2/resource/{rtype}/{rid}")
