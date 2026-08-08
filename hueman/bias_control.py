"""Pure decision core for the daemon's TV bias hold (no clock, no I/O).

Given the parsed :class:`~hueman.config.BiasSpec`, whether the TV is on, and the
current circadian curve sample, :func:`bias_actions` decides the per-light action
for this tick. :class:`TriggerAggregator` folds on/off edges from the (I/O)
trigger sources into a single ``tv_on`` signal with optional debounce. This
mirrors the pure/I-O split of :mod:`hueman.circadian_control`: everything here is
a pure function of explicit inputs, so it is fully unit-tested without a bridge.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .circadian_control import DriveTo
from .config import BiasSpec, LightState


def unknown_bias_lights(spec: BiasSpec, available_names: Iterable[str]) -> list[str]:
    """Return the bias light names not present on the bridge, in declared order.

    The daemon resolves each bias light name to a bridge id at startup; a typo'd
    or not-yet-created viewing light otherwise raises on the *first* bad name,
    leaving the operator to fix them one crash-loop at a time. Passing
    ``state.all_light_names`` here surfaces the complete set up front.
    """
    known = set(available_names)
    return [light.name for light in spec.lights if light.name not in known]


@dataclass(frozen=True)
class BiasHold:
    """Hold a viewing light at its static bias look (TV on)."""

    light: str
    look: LightState
    transition_ms: int


@dataclass(frozen=True)
class BiasDrive:
    """Drive a viewing light to the current circadian sample (TV off, idle=circadian)."""

    light: str
    brightness: float
    mirek: int
    transition_ms: int


@dataclass(frozen=True)
class BiasOff:
    """Fade a viewing light off (TV off; idle=off, or out of window/no curve)."""

    light: str
    transition_ms: int


def bias_actions(
    spec: BiasSpec,
    *,
    tv_on: bool,
    in_window: bool,
    curve: DriveTo | None,
    night_look: LightState | None = None,
    transition_ms: int,
    fade_off_ms: int,
    edge: bool = False,
) -> list[BiasHold | BiasDrive | BiasOff]:
    """Return the per-light action for each bias light this tick.

    * ``tv_on`` -> every light holds its ``look`` — the only case any light is
      handled *differently* from the rest of the home; this is a TV-on-only
      override, not a standing "viewing set" identity.
    * otherwise ``idle == "circadian"``: join whatever the rest of the home is
      doing. In window with a ``curve`` sample -> follow the curve (same
      brightness/mirek as the main set). Out of window (or in window with no
      curve sample) and ``night_look`` is configured -> hold that same look,
      same as the main zone's own parked state — a circadian-idle light must
      never go dark on its own just because the window closed; it should read
      as part of one home, not a separate "viewing" island (observed live
      2026-08-08: TV-viewing lights going fully off overnight while the rest
      of the home sat at a dim night_look read as broken, not "TV is off").
    * otherwise (``idle == "off"``, or ``idle == "circadian"`` with no
      ``night_look`` and no curve) -> fade off.

    ``edge`` marks a committed TV-state flip: every action then fades over the
    short ``spec.transition_ms`` instead of the steady-state ``transition_ms``/
    ``fade_off_ms``, which are tuned for imperceptible drift, not a mode flip.
    """
    fade = spec.transition_ms if edge else transition_ms
    off_fade = spec.transition_ms if edge else fade_off_ms
    actions: list[BiasHold | BiasDrive | BiasOff] = []
    for light in spec.lights:
        if tv_on:
            actions.append(BiasHold(light.name, light.look, fade))
        elif light.idle == "circadian" and in_window and curve is not None:
            actions.append(BiasDrive(light.name, curve.brightness, curve.mirek, fade))
        elif light.idle == "circadian" and night_look is not None:
            actions.append(BiasHold(light.name, night_look, fade))
        else:
            actions.append(BiasOff(light.name, off_fade))
    return actions


class TriggerAggregator:
    """OR-combines on/off edges from N trigger sources into one debounced ``tv_on``.

    Each source is tracked by name; the raw signal is "any source on". The
    committed signal only follows the raw one once it has held for ``debounce_ms``
    (so a quick on/off bounce never flips it). With ``debounce_ms == 0`` edges
    take effect immediately. Pure: the caller injects ``now`` (epoch seconds).
    """

    def __init__(self, debounce_ms: int = 0) -> None:
        """Start with no active sources; edges must hold ``debounce_ms`` to commit."""
        self._debounce_s = debounce_ms / 1000.0
        self._active: set[str] = set()   # sources currently reporting on
        self._committed = False
        self._raw = False
        self._raw_since: float | None = None
        self.last_source: str | None = None   # source that caused the last raw flip

    def on(self, now: float, source: str = "default") -> None:
        """Record an on-edge from ``source`` at ``now`` (idempotent per source)."""
        self._active.add(source)
        self._recompute(now, source)

    def off(self, now: float, source: str = "default") -> None:
        """Record an off-edge from ``source`` at ``now`` (idempotent per source)."""
        self._active.discard(source)
        self._recompute(now, source)

    def reset(self, now: float) -> None:
        """Drop every source's on-signal: the consumer ended the episode itself
        (e.g. security max-duration), whichever source had armed it. With a
        nonzero debounce the committed signal follows only after the debounce
        window, like any other edge."""
        self._active.clear()
        self._recompute(now, "reset")

    def _recompute(self, now: float, source: str) -> None:
        """Refresh the raw OR-signal after an edge from ``source``.

        On a flip, restart the debounce clock and credit ``source`` as the
        cause; edges that don't flip the signal leave both alone.
        """
        raw = bool(self._active)
        if raw != self._raw:
            self._raw = raw
            self._raw_since = now
            self.last_source = source

    def tv_on(self, now: float) -> bool:
        """Return the committed TV-on signal as of ``now``.

        Commits a pending raw flip once it has held for the debounce window;
        with ``debounce_ms == 0`` this is the raw signal itself.
        """
        if (
            self._raw != self._committed
            and self._raw_since is not None
            and now - self._raw_since >= self._debounce_s
        ):
            self._committed = self._raw
        return self._committed

    def active(self, now: float) -> bool:
        """Generic alias of :meth:`tv_on` for non-TV consumers (e.g. security)."""
        return self.tv_on(now)
