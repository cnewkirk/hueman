"""Pure decision core for the daemon's night-guide overlay (no clock, no I/O).

Motion-triggered path lighting for when circadian isn't actively driving the
zone: a brief soft-red "guide" look while someone's up at night, then a clean
hand-back once the motion stops. Everything here is a pure function of
explicit inputs, so it is fully unit-tested without a bridge, mirroring
:mod:`hueman.bias_control` and :mod:`hueman.security_control`.

This module owns only the timing (when does an episode start/end); the daemon
decides *what* to show and *how* to hand back (an exact snapshot restore for a
real manual override, or a recomputed circadian/rest target otherwise — there
is no bridge state here to make that call from).
"""

from __future__ import annotations

IDLE = "idle"
GUIDING = "guiding"


class NightGuideController:
    """Tracks one guide episode: IDLE until motion, GUIDING until ``timeout``
    with no further motion.

    Repeated motion while GUIDING extends the episode (the timeout is always
    measured from the *last* motion, not the first) — the guide light should
    stay on for as long as someone's actually moving around, not blink off on
    a fixed clock mid-trip.
    """

    def __init__(self, timeout_ms: int) -> None:
        """Bind the no-motion timeout (in ms) that ends a guiding episode."""
        self._timeout_s = timeout_ms / 1000.0
        self._state = IDLE
        self._last_motion: float | None = None

    @property
    def state(self) -> str:
        """Return the current state (``IDLE`` or ``GUIDING``)."""
        return self._state

    def motion(self, now: float) -> bool:
        """Record motion at ``now``; extends or starts a guiding episode.

        Returns:
            ``True`` if this motion is a fresh IDLE -> GUIDING edge (the
            caller should show the guide look); ``False`` if it just extended
            an episode already in progress (nothing new to write).
        """
        entering = self._state == IDLE
        self._state = GUIDING
        self._last_motion = now
        return entering

    def tick(self, now: float) -> bool:
        """Advance the timeout clock; caller should poll this periodically.

        Returns:
            ``True`` exactly on the GUIDING -> IDLE edge (the episode just
            ended — the caller should hand control back). ``False`` otherwise
            (already IDLE, or still within ``timeout`` of the last motion).
        """
        if (
            self._state == GUIDING
            and self._last_motion is not None
            and now - self._last_motion >= self._timeout_s
        ):
            self._state = IDLE
            return True
        return False
