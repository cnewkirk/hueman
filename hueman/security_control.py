"""Pure decision core for the daemon's security mode (no clock, no I/O).

Given the parsed :class:`~hueman.config.SecuritySpec` and an injected elapsed
time + frame index, decides the light frame for an escalating ALERT -> CHAOS
show. The safety cap is **luminance-only**: a unit's brightness changes no
faster than ``min_flash_interval`` (out of the 3-30 Hz photosensitive-seizure
band), while hue snaps violently on *every* update — golden-angle jumps, so
consecutive colours are always far apart, never neighbouring shades. When the
controller is built with the member ``lights``, chaos drives individual lights
in a rotating budget (``lights_per_frame``) instead of whole groups, so the
apartment churns as a decorrelated patchwork. The ALERT phase stays whole-group
and legible. Everything here is a pure function of explicit inputs, so it is
fully unit-tested without a bridge, mirroring :mod:`hueman.bias_control` and
:mod:`hueman.circadian_control`.
"""

from __future__ import annotations

import colorsys
import math
from collections.abc import Iterable
from dataclasses import dataclass

from .config import SecuritySpec
from .engine import TargetState

PHASE_ALERT = "alert"
PHASE_CHAOS = "chaos"

# CHAOS brightness levels (percent). Out-of-phase alternation swings each unit
# between these. The trough is near-black on purpose — the harsher the contrast
# per (cap-limited) flip, the more jarring the show; the flip RATE stays floored
# by min_flash_interval, which is what the photosensitive-safety cap governs.
_CHAOS_BRIGHT = 100.0
_CHAOS_DIM = 3.0
# Every Nth update of a unit is a full-white slam instead of a saturated hue —
# a desaturated blast against the colour field is the most jarring cut REST can
# deliver. Offset per unit so the room never blasts in unison.
_WHITE_BLAST_EVERY = 3
# Chaos alternates sub-patterns on this cadence: per-light PATCHWORK, then a
# whole-zone counter-phase STROBE, and back. The rhythm change is itself
# disorienting; steady-state anything becomes readable within seconds.
_PATTERN_MS = 3000


def unknown_security_groups(spec: SecuritySpec, available_names: Iterable[str]) -> list[str]:
    """Return the security group names not present on the bridge, in declared order."""
    known = set(available_names)
    return [g for g in spec.groups if g not in known]


@dataclass(frozen=True)
class FrameTarget:
    """One unit's target for a single security frame.

    ``kind`` is ``"group"`` (name -> grouped_light) or ``"light"`` (name is the
    light's opaque unit id — the daemon passes light rids directly).
    """

    kind: str
    name: str
    target: TargetState


@dataclass(frozen=True)
class SecurityFrame:
    """The whole-home target for one security frame."""

    phase: str                       # PHASE_ALERT | PHASE_CHAOS
    targets: tuple[FrameTarget, ...]


# Golden-ratio conjugate: successive multiples mod 1 are maximally spread, so a
# unit's consecutive hue updates always jump ~137.5 degrees — hard complementary
# cuts, deterministic, and never a repeating short cycle.
_GOLDEN = 0.6180339887498949 - 0.2360679774997897  # 0.381966...


def _hsv_hex(hue: float) -> str:
    """Convert a hue (0..1) at full saturation/value to a 6-digit hex string."""
    r, g, b = colorsys.hsv_to_rgb(hue % 1.0, 1.0, 1.0)
    return f"{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


class SecurityController:
    """Decides each security frame: ALERT breathe, then luminance-capped CHAOS."""

    def __init__(self, spec: SecuritySpec, *, lights: tuple[str, ...] = (), seed: int = 1) -> None:
        """Bind the spec; ``lights`` (member unit ids) enables per-light chaos.

        ``seed`` perturbs each unit's hue offset so distinct runs (or tests)
        get different but deterministic colour sequences.
        """
        self._spec = spec
        self._lights = tuple(lights)
        self._seed = seed
        self._alert_ms = spec.alert_seconds * 1000
        self._flash_period_frames = max(
            1, math.ceil(spec.min_flash_interval_ms / spec.frame_interval_ms)
        )

    def phase_at(self, elapsed_ms: float) -> str:
        """Return ``PHASE_ALERT`` until ``alert_seconds`` elapse, then ``PHASE_CHAOS``."""
        return PHASE_ALERT if elapsed_ms < self._alert_ms else PHASE_CHAOS

    def is_expired(self, elapsed_ms: float) -> bool:
        """Return ``True`` once the show has run for ``max_duration_ms`` or longer."""
        return elapsed_ms >= self._spec.max_duration_ms

    def frame_at(self, elapsed_ms: float, frame_index: int) -> SecurityFrame:
        """Return the frame for this instant, dispatching on the current phase.

        ``elapsed_ms`` selects the phase (wall time since arming); ``frame_index``
        is the daemon's monotonically increasing frame counter, which drives the
        chaos rotation and flash-cap quantisation.
        """
        if self.phase_at(elapsed_ms) == PHASE_ALERT:
            return self._alert_frame(elapsed_ms)
        return self._chaos_frame(elapsed_ms, frame_index)

    def _alert_frame(self, elapsed_ms: float) -> SecurityFrame:
        """Return the ALERT frame: a slow synchronized whole-group breathe.

        Cosine wave between ``alert_min_brightness`` and 100 percent (trough at
        ``t=0``) in the configured alert colour — deliberately legible, unlike
        the chaos phase.
        """
        seconds = elapsed_ms / 1000.0
        wave = 0.5 - 0.5 * math.cos(2 * math.pi * self._spec.alert_breathe_hz * seconds)
        lo = self._spec.alert_min_brightness
        bri = round(lo + (100.0 - lo) * wave, 1)
        target = TargetState(on=True, brightness=bri, hex=self._spec.alert_color)
        return SecurityFrame(
            phase=PHASE_ALERT,
            targets=tuple(FrameTarget("group", g, target) for g in self._spec.groups),
        )

    def _chaos_frame(self, elapsed_ms: float, frame_index: int) -> SecurityFrame:
        """Per-unit chaos under the luminance cap, alternating two sub-patterns.

        PATCHWORK: decorrelated per-light churn (rotating write budget).
        STROBE: whole-zone counter-phase luminance pumping at the flash-period
        cadence — the whole visual field slams bright<->near-black in opposition
        while the zone colours churn. Patterns swap every ``_PATTERN_MS``.

        Per-light mode (member lights injected): each frame updates a rotating
        slice of ``lights_per_frame`` lights, so every light is refreshed once
        per ``period`` frames and total write rate stays within the bridge's
        REST budget. Group mode (no lights): every group updates every frame.

        Safety cap: a unit's *brightness* flips only every
        ``ceil(flash_period / period)`` of its own updates — i.e. never faster
        than ``min_flash_interval`` in wall time — while its hue jumps by the
        golden angle on every single update.
        """
        chaos_elapsed = max(0.0, elapsed_ms - self._alert_ms)
        # The opening slam is the FIRST chaos frame, detected by elapsed time
        # (frame_index can drift from wall time under loop jitter).
        first_chaos = chaos_elapsed < self._spec.frame_interval_ms
        if (
            self._lights
            and len(self._spec.groups) >= 2
            and (int(chaos_elapsed) // _PATTERN_MS) % 2 == 1
            and not first_chaos                      # the opening slam wins
        ):
            return self._strobe_frame(frame_index)
        slam = False
        if self._lights:
            units, kind = self._lights, "light"
            n = len(units)
            per_frame = min(self._spec.lights_per_frame, n)
            period = math.ceil(n / per_frame)          # frames per full rotation
            if first_chaos:
                slam = True                            # opening slam: one whole-home bang
                picks = units
            else:
                slot = frame_index % period
                picks = units[slot * per_frame:(slot + 1) * per_frame]
        else:
            units, kind = self._spec.groups, "group"
            picks = units
            per_frame = len(units)
            period = 1
        bri_every = max(1, math.ceil(self._flash_period_frames / period))
        targets: list[FrameTarget] = []
        for name in picks:
            i = units.index(name)
            # A unit's own update count. On the slam frame, lights whose rotation
            # slot has not come up yet use their PREVIOUS ordinal for brightness —
            # the slam bangs colour everywhere but never flips a light's luminance
            # early (the flash cap is wall-time, and a light updated the frame
            # before the slam must not flip 1 frame later).
            update_ordinal = frame_index // period
            bri_ordinal = update_ordinal
            if slam and (frame_index % period) < (i // per_frame):
                bri_ordinal = max(0, update_ordinal - 1)
            # Brightness flips every bri_every updates of a unit, which is
            # >= flash_period frames of wall time regardless of rotation.
            bright = (bri_ordinal // bri_every + i) % 2 == 0
            bri = _CHAOS_BRIGHT if bright else _CHAOS_DIM
            if (update_ordinal + i) % _WHITE_BLAST_EVERY == _WHITE_BLAST_EVERY - 1:
                hexv = "ffffff"                        # white slam
            else:
                offset = (hash((self._seed, name)) & 0xFFFF) / 0xFFFF
                hexv = _hsv_hex(offset + update_ordinal * _GOLDEN)
            targets.append(FrameTarget(kind, name, TargetState(on=True, brightness=bri, hex=hexv)))
        return SecurityFrame(phase=PHASE_CHAOS, targets=tuple(targets))

    def _strobe_frame(self, frame_index: int) -> SecurityFrame:
        """Whole-zone counter-phase strobe at the flash-period cadence.

        Flip timing is quantised to whole flash periods of frames, so no zone's
        luminance ever toggles faster than ``min_flash_interval`` — the same
        tested wall-time invariant as the patchwork. With toggles counted
        pair-wise (a flash = a full bright->dark->bright cycle) this sits well
        inside the <3 flashes/second photosensitive threshold; the intensity
        comes from the whole field pumping in opposition, not from rate.
        """
        flip = frame_index // self._flash_period_frames
        targets: list[FrameTarget] = []
        for i, g in enumerate(self._spec.groups):
            bright = (flip + i) % 2 == 0
            bri = _CHAOS_BRIGHT if bright else _CHAOS_DIM
            offset = (hash((self._seed, g)) & 0xFFFF) / 0xFFFF
            hexv = _hsv_hex(offset + flip * _GOLDEN)
            targets.append(FrameTarget("group", g, TargetState(on=True, brightness=bri, hex=hexv)))
        return SecurityFrame(phase=PHASE_CHAOS, targets=tuple(targets))
