"""Translation between engine :class:`TargetState` values and CLIP API bodies.

Kept separate from both the engine (which should not know API wire formats) and
the network client (which should not know colour science). Everything here is a
pure function of its inputs, so the colour conversion and body construction are
unit-testable without a bridge.
"""

from __future__ import annotations

from .engine import TargetState


class ColorConverter:
    """Converts sRGB hex colours to the CIE xy space the CLIP API expects."""

    @staticmethod
    def _linearize(channel: float) -> float:
        """Apply inverse sRGB companding to a 0-1 channel value."""
        if channel > 0.04045:
            return ((channel + 0.055) / 1.055) ** 2.4
        return channel / 12.92

    @classmethod
    def hex_to_xy(cls, hex_color: str) -> tuple[float, float]:
        """Convert a 6-digit hex colour to a CIE ``(x, y)`` chromaticity pair.

        Args:
            hex_color: Six hex digits, with or without a leading ``#``.

        Returns:
            The ``(x, y)`` chromaticity, each rounded to four decimals.
        """
        cleaned = hex_color.lstrip("#")
        red = int(cleaned[0:2], 16) / 255.0
        green = int(cleaned[2:4], 16) / 255.0
        blue = int(cleaned[4:6], 16) / 255.0

        red_lin = cls._linearize(red)
        green_lin = cls._linearize(green)
        blue_lin = cls._linearize(blue)

        # Wide-gamut RGB -> XYZ matrix used by Philips' own reference code.
        big_x = red_lin * 0.664511 + green_lin * 0.154324 + blue_lin * 0.162028
        big_y = red_lin * 0.283881 + green_lin * 0.668433 + blue_lin * 0.047685
        big_z = red_lin * 0.000088 + green_lin * 0.072310 + blue_lin * 0.986039

        total = big_x + big_y + big_z
        if total == 0:
            return (0.0, 0.0)
        return (round(big_x / total, 4), round(big_y / total, 4))


def _target_body(target: TargetState, transition_ms: int | None = None) -> dict:
    """Build the CLIP on/dimming/colour body for ``target``.

    Resource-agnostic: the same body shape applies to ``grouped_light`` and
    ``light`` writes. When ``transition_ms`` is given, a ``dynamics.duration`` is
    attached so the bridge fades to the target (used by the circadian daemon for
    continuous transitions, including fade-to-off).
    """
    if not target.on:
        body: dict = {"on": {"on": False}}
        if transition_ms is not None:
            body["dynamics"] = {"duration": transition_ms}
        return body

    body = {"on": {"on": True}}
    if target.brightness is not None:
        body["dimming"] = {"brightness": round(target.brightness, 1)}
    if target.hex is not None:
        x, y = ColorConverter.hex_to_xy(target.hex)
        body["color"] = {"xy": {"x": x, "y": y}}
    elif target.mirek is not None:
        body["color_temperature"] = {"mirek": target.mirek}
    if transition_ms is not None:
        body["dynamics"] = {"duration": transition_ms}
    return body


class GroupedLightCommand:
    """Builds a ``grouped_light`` PUT body from an engine target state."""

    @staticmethod
    def build(target: TargetState, transition_ms: int | None = None) -> dict:
        """Return the CLIP body to PUT to ``/clip/v2/resource/grouped_light/<id>``."""
        return _target_body(target, transition_ms)


class LightCommand:
    """Builds a single ``light`` PUT body (per-light writes, e.g. the bias set).

    The CLIP ``light`` resource accepts the same on/dimming/colour/`dynamics`
    body as ``grouped_light``, so this shares :func:`_target_body`.
    """

    @staticmethod
    def build(target: TargetState, transition_ms: int | None = None) -> dict:
        """Return the CLIP body to PUT to ``/clip/v2/resource/light/<id>``."""
        return _target_body(target, transition_ms)
