"""Exception hierarchy for hueman.

Everything user-facing inherits from :class:`HueIacError` so the CLI can catch a
single type, print a clean message, and exit non-zero without a traceback.
"""

from __future__ import annotations


class HueIacError(Exception):
    """Base class for all expected, user-actionable failures."""


class ConfigError(HueIacError):
    """The IaC configuration file is missing, malformed, or semantically invalid."""


class BridgeError(HueIacError):
    """The bridge returned an error or could not be reached."""


class AuthError(BridgeError):
    """Authentication failed or no application key is available."""


class PinError(BridgeError):
    """The bridge's TLS certificate did not match the pinned fingerprint."""
