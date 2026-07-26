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
    """The bridge returned an error or could not be reached.

    ``unreachable`` marks the specific case where the bridge accepted the call
    but reported that the *target device* has Zigbee communication problems
    (e.g. a bulb a housekeeper unplugged). That is not a transient bridge
    rejection to retry — the device is simply gone — so callers driving a set
    of lights can treat it as "skip this one" rather than "the whole write
    failed", and not stall on a bulb that will never answer.
    """

    def __init__(self, *args: object, unreachable: bool = False) -> None:
        """Build a bridge error; ``unreachable`` flags a dead-device report."""
        super().__init__(*args)
        self.unreachable = unreachable


class AuthError(BridgeError):
    """Authentication failed or no application key is available."""


class PinError(BridgeError):
    """The bridge's TLS certificate did not match the pinned fingerprint."""
