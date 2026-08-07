"""Runtime that drives the circadian curve onto a zone from a long-lived process.

Where ``apply`` provisions native bridge resources, this module is the *daemon*
alternative: a persistent process that, every tick, asks the pure
:class:`~hueman.circadian_control.CircadianController` what the zone should look
like *right now* and writes that to the zone's ``grouped_light`` service with a
smooth transition. It mirrors :class:`hueman.watch.MotionController` almost
exactly — a tick thread plus an SSE event loop and a capped-backoff reconnect
loop — but for one zone and the circadian state machine instead of per-area
motion engines.

All decision logic lives in the (tested, I/O-free) controller; all that remains
here is plumbing: clock, HTTP writes, event decoding, the settle-and-compare
override detector, and the control-file poll that lets an external trigger
resume after a manual override.

Manual-override detection is *settle-and-compare*, not echo-based. A single
transition write (a 75s fade) makes the bridge emit a STREAM of ``grouped_light``
brightness events — the ramp plus periodic re-emits of the settled value — and
because the transition is longer than the tick interval the zone is perpetually
mid-fade. A brightness that is still *moving* is therefore presumed to be a fade
(ours or anyone's) and is NOT classified. Only once the observed brightness has
*held* (within ``settle_epsilon``) for ``settle_window_ms`` is it compared to the
daemon's last commanded target: settled within ``override_band`` of target is our
own look (no-op); settled beyond the band, or the zone turned off while we expect
it on, is a human override and the controller suspends. A power cycle
(off->on while suspended) re-engages the daemon when ``resume_on_power_cycle`` is
set.

Classification is not left to the event stream alone — that would race the tick.
The tick judges any settled-but-unjudged value itself (once its own last fade has
had time to land, so a stale mid-fade sample is never misread), and it *defers*
its write whenever the zone sits on an unjudged value beyond ``override_band`` of
target. Without both, a tick landing between a manual change and the bridge's
re-emission of the settled value would silently fade the change back to the curve
and then classify its own landing as "self" — the human loses, with no trace.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import random
from concurrent.futures import ThreadPoolExecutor
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import cast
from zoneinfo import ZoneInfo

import requests

from .bias_control import (
    BiasDrive,
    BiasHold,
    BiasOff,
    TriggerAggregator,
    bias_actions,
    unknown_bias_lights,
)
from .circadian_control import CircadianController, DriveTo, FadeOff, Hold
from .client import HueClient
from .config import CircadianDaemonSpec, Config, RhythmSpec
from .engine import TargetState
from .errors import AuthError, BridgeError, ConfigError
from .payload import GroupedLightCommand, LightCommand
from .presence import ActivityEvent, PresenceTracker
from .rhythm_control import AnchorStore, RhythmEngine, SignalState
from .security_control import SecurityController, unknown_security_groups
from .state import BridgeState
from .sun import SolarCalculator
from .watch import BridgeEvent, HueEventStream

_LOG = logging.getLogger("hueman.circadian_daemon")


class _BiasWrite(Enum):
    """Outcome of a single bias light write, for the edge-latch decision.

    ``OK`` — the bridge accepted it. ``FAILED`` — a *transient* bridge rejection
    (e.g. "command queue is full"); the edge must not latch so the next tick
    retries it. ``UNREACHABLE`` — the device reported Zigbee communication
    problems (an unplugged bulb); it will not answer no matter how often we
    retry, so it must NOT block the edge from latching — otherwise one dead
    viewing light makes every tick look like a fresh TV flip and the daemon
    re-fires the whole bias set every poll (observed live 2026-07-25).
    """

    OK = "ok"
    FAILED = "failed"
    UNREACHABLE = "unreachable"


class CircadianDaemon:
    """Drives one zone's circadian curve and honours manual overrides.

    Args:
        client: An authenticated bridge client.
        state: A loaded :class:`~hueman.state.BridgeState` (used to resolve the
            zone's ``grouped_light`` id and an optional resume-trigger scene).
        config: The parsed IaC configuration; ``config.circadian_daemon`` must
            be present.
        clock: Callable returning epoch seconds; injectable for testing.
        sleep: Sleep function used for reconnect backoff; injectable for testing.
        stream_factory: Builds the SSE stream each (re)connect; defaults to a
            :class:`~hueman.watch.HueEventStream` over ``client``.
    """

    # Attributes assigned on both construction paths (``__init__`` and
    # ``_setup``); annotated once here so the two assignment sites agree.
    _bias_sse_on_rid: str | None
    _bias_sse_off_rid: str | None
    _bias_rids: dict[str, str]
    _security_sse_on_rid: str | None
    _security_sse_off_rid: str | None
    _security_rids: dict[str, str]
    _security_light_rids: tuple[str, ...]
    _rhythm_motion_rooms: dict[str, str]

    def __init__(
        self,
        client: HueClient,
        state: BridgeState,
        config: Config,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        stream_factory: Callable[[], "HueEventStream"] | None = None,
    ) -> None:
        """Resolve bridge rids from ``state``, validate the config, and wire up."""
        spec = self._require_spec(config)
        rid = state.group(spec.zone).grouped_light_rid
        if rid is None:
            raise BridgeError(f"zone {spec.zone!r} has no grouped_light service")
        self._setup(client, config, rid, clock=clock, sleep=sleep, stream_factory=stream_factory)
        self._resume_trigger_rid = self._resolve_resume_trigger(state, spec.resume_trigger)
        if spec.bias is not None:
            missing = unknown_bias_lights(spec.bias, state.all_light_names)
            if missing:
                raise ConfigError(
                    "circadian_daemon.bias references light(s) not on the bridge: "
                    + ", ".join(repr(name) for name in missing)
                    + " — run `hueman inventory` to list available light names"
                )
            self._bias_sse_on_rid = self._resolve_resume_trigger(state, spec.bias.sse_on)
            self._bias_sse_off_rid = self._resolve_resume_trigger(state, spec.bias.sse_off)
            self._bias_rids = {bl.name: state.light(bl.name).light_rid for bl in spec.bias.lights}
        sec = config.security
        if sec is not None:
            missing = unknown_security_groups(sec, state.group_names)
            if missing:
                raise ConfigError(
                    "security.groups references group(s) not on the bridge: "
                    + ", ".join(repr(g) for g in missing)
                    + " — run `hueman inventory` to list available group names"
                )
            no_gl = [g for g in sec.groups if state.group(g).grouped_light_rid is None]
            if no_gl:
                raise BridgeError(
                    "security.groups has group(s) with no grouped_light service: "
                    + ", ".join(repr(g) for g in no_gl)
                )
            if len(sec.groups) < 2:
                _LOG.warning("security.groups has <2 groups; chaos zone alternation is disabled")
            # The ``no_gl`` raise above guarantees every group has a rid; the
            # walrus filter just narrows ``str | None`` -> ``str`` for the checker.
            self._security_rids = {
                g: gl_rid
                for g in sec.groups
                if (gl_rid := state.group(g).grouped_light_rid) is not None
            }
            # Member-light rids across the security groups: chaos drives these
            # individually (decorrelated patchwork) instead of two group blobs.
            seen: set[str] = set()
            light_rids: list[str] = []
            for g in sec.groups:
                for rid in state.group(g).light_rids:
                    if rid not in seen:
                        seen.add(rid)
                        light_rids.append(rid)
            self._security_light_rids = tuple(light_rids)
            self._rebuild_security_controller()
            self._security_sse_on_rid = self._resolve_resume_trigger(state, sec.sse_on)
            self._security_sse_off_rid = self._resolve_resume_trigger(state, sec.sse_off)
        if config.rhythm is not None:
            self._rhythm_motion_rooms = {
                srid: (area.room_name or area.name)
                for area in state.motion_areas
                for srid in area.service_rids
            }
            if not self._rhythm_motion_rooms:
                _LOG.warning(
                    "rhythm: no MotionAware areas found on the bridge — presence "
                    "inference will see only manual-override evidence")
        self._configure_logging(spec)

    @classmethod
    def for_test(
        cls, client: HueClient, config: Config, *, grouped_light_rid: str
    ) -> "CircadianDaemon":
        """Build a daemon wired to a pre-resolved rid, bypassing ``BridgeState``.

        The real constructor needs a loaded bridge to resolve the zone's
        ``grouped_light`` id and any resume-trigger scene; tests skip both and
        inject the rid directly, exercising :meth:`_tick_once` /
        :meth:`_handle_event` without a network. Logging is left unconfigured so
        a test never touches the filesystem.
        """
        self = cls.__new__(cls)
        self._setup(client, config, grouped_light_rid)
        # Resume-trigger resolution needs BridgeState; there is none in tests
        # (and the spec's resume_trigger is None there), so it can never match.
        self._resume_trigger_rid = None
        return self

    def _setup(
        self,
        client: HueClient,
        config: Config,
        rid: str,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        stream_factory: Callable[[], "HueEventStream"] | None = None,
    ) -> None:
        """Shared construction for both the real and the test entry points."""
        spec = self._require_spec(config)
        loc = config.location
        solar = SolarCalculator(loc.lat, loc.lon, loc.tz_offset_hours, tz=loc.tz)
        self._solar = solar
        self._client = client
        self._config = config
        self._spec = spec
        self._clock = clock
        self._sleep = sleep
        self._controller = CircadianController(
            spec, config.circadian, solar, loc.tz_offset_hours, tz=loc.tz
        )
        self._rid = rid
        # --- settle-and-compare override state (lock-guarded; both threads
        # touch it: the tick thread sets _cmd_* on each write, the SSE loop reads
        # them and updates _obs_*). ---
        self._cmd_brightness: float | None = None   # last commanded target brightness
        self._cmd_on: bool = True                   # last commanded on-state (False after fade-off)
        self._obs_brightness: float | None = None   # last observed brightness
        self._obs_since: float = 0.0                # when _obs_brightness last MOVED (epoch)
        self._obs_classified: bool = False          # current settled value already classified?
        self._classify_grace_until: float = 0.0     # no override classification before this (epoch)
        self._cmd_fade_until: float = 0.0           # when our own last commanded fade lands (epoch)
        self._last_on: bool | None = None           # last observed on-state (power-cycle)
        self._override_band = spec.override_band
        self._settle_window_s = spec.settle_window_ms / 1000.0
        self._settle_epsilon = spec.settle_epsilon
        # Guards the controller's mode and the settle state above, which the tick
        # thread and the SSE loop both mutate.
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._stream_factory = stream_factory or (lambda: HueEventStream(self._client))
        # --- TV bias hold (optional) ---
        # The aggregate debounce only matters for the flap-prone probe source;
        # explicit sse/control-file edges use 0 (immediate) unless probe is on.
        bias = spec.bias
        self._bias = bias
        self._bias_aggregator = TriggerAggregator(
            debounce_ms=(bias.probe_debounce_ms if (bias and bias.probe_enabled) else 0)
        )
        self._bias_sse_on_rid = None                # resolved in __init__ (needs BridgeState)
        self._bias_sse_off_rid = None
        self._bias_rids = {}                        # bias light name -> light_rid
        self._bias_probe_thread: threading.Thread | None = None
        self._bias_file_thread: threading.Thread | None = None
        # Last committed tv_on we successfully drove the bias set to. Lets the
        # probe thread apply bias the instant the TV state flips (edge), instead
        # of waiting up to one ``interval`` for the next curve tick. ``None`` =
        # never applied — the first apply is treated as an edge, and a failed
        # edge apply is NOT latched so the next apply retries it.
        self._bias_last_applied_on: bool | None = None
        # Light rids whose off has been written successfully; suppresses the
        # redundant per-tick off re-PUTs (7 writes/min overnight, observed to
        # contribute to bridge 'command queue is full' rejections).
        self._bias_off_written: set[str] = set()
        # --- security mode (optional, top priority) ---
        self._security = config.security
        self._security_controller = (
            SecurityController(config.security) if config.security is not None else None
        )
        self._security_aggregator = TriggerAggregator(debounce_ms=0)  # panic fires instantly
        # Read/written without the lock intentionally: benign under the GIL, self-corrects within one tick.
        self._security_active = False
        self._security_last_phase: str | None = None
        self._security_sse_on_rid = None
        self._security_sse_off_rid = None
        self._security_rids = {}                      # group name -> grouped_light rid
        self._security_light_rids = ()                # member lights (chaos units)
        self._security_thread: threading.Thread | None = None
        # --- rhythm engine (optional, observe stage only) ---
        self._rhythm: RhythmSpec | None = config.rhythm
        self._presence: PresenceTracker | None = None
        self._rhythm_engine: RhythmEngine | None = None
        self._rhythm_tz: ZoneInfo | None = None
        self._rhythm_motion_rooms = {}   # *_area_motion service rid -> room
        if self._rhythm is not None:
            if self._rhythm.stage != "observe":
                raise ConfigError(
                    f"rhythm.stage {self._rhythm.stage!r} is not implemented yet; "
                    "only 'observe' ships in stage 1 — see the rhythm design spec")
            if loc.tz is None:
                raise ConfigError(
                    "rhythm requires location.tz (an IANA timezone name) for "
                    "wall-clock day-phase reasoning; tz_offset_hours alone is not enough")
            self._presence = PresenceTracker(self._rhythm.presence)
            self._rhythm_tz = ZoneInfo(loc.tz)
            self._rhythm_engine = RhythmEngine(
                self._rhythm, self._load_anchor_store(), tz=loc.tz)

    def _rebuild_security_controller(self) -> None:
        """(Re)build the controller with the resolved member lights, so chaos
        runs per-light; group-mode fallback when no lights are resolved."""
        if self._security is not None:
            self._security_controller = SecurityController(
                self._security, lights=self._security_light_rids
            )

    @staticmethod
    def _require_spec(config: Config) -> CircadianDaemonSpec:
        """Return the daemon spec or fail clearly if the config lacks one."""
        spec = config.circadian_daemon
        if spec is None:
            raise BridgeError("config has no 'circadian_daemon' section")
        return spec

    @staticmethod
    def _resolve_resume_trigger(state: BridgeState, resume_trigger: str | None) -> str | None:
        """Resolve a configured resume-trigger name to a resource id.

        ``resume_trigger`` names a scene (or button) whose recall re-engages the
        daemon after a manual override. Resolved once at startup: a matching
        scene's id is used; otherwise the raw string is treated as a literal rid
        so an explicit id in YAML still works. ``None`` disables the trigger.
        """
        if not resume_trigger:
            return None
        scene = state.scene(resume_trigger)
        if scene is not None and "id" in scene:
            # CLIP resource ids are always strings; the cast only informs the
            # checker (the raw payload is dict[str, Any]).
            return cast(str, scene["id"])
        return resume_trigger

    def _configure_logging(self, spec: CircadianDaemonSpec) -> None:
        """Point the module logger at the configured file path and level.

        Attaches both a StreamHandler on sys.stdout (so ``docker logs`` sees
        normal operation) and a FileHandler at ``spec.log_path``.  Both share the
        same formatter.  Duplicate-guards prevent double-attachment if called more
        than once (e.g. in tests).  Best-effort: a logging-setup failure (e.g. an
        unwritable path) must not prevent the daemon from running, so it is
        swallowed with a warning.
        """
        try:
            level = getattr(logging, spec.log_level.upper(), logging.INFO)
            _LOG.setLevel(level)
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

            # --- stdout handler (for docker logs / systemd journal) ---
            # FileHandler subclasses StreamHandler, so use ``type(h) is`` to
            # distinguish a plain stdout StreamHandler from a FileHandler.
            already_stdout = any(
                type(h) is logging.StreamHandler and h.stream is sys.stdout
                for h in _LOG.handlers
            )
            if not already_stdout:
                stdout_handler = logging.StreamHandler(sys.stdout)
                stdout_handler.setFormatter(formatter)
                _LOG.addHandler(stdout_handler)

            # --- file handler ---
            path = spec.log_path
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            already = any(
                isinstance(h, logging.FileHandler)
                and getattr(h, "baseFilename", None) == os.path.abspath(path)
                for h in _LOG.handlers
            )
            if not already:
                handler = logging.FileHandler(path)
                handler.setFormatter(formatter)
                _LOG.addHandler(handler)
        except OSError as e:  # pragma: no cover - filesystem-dependent
            _LOG.warning("could not configure log file %s: %s", spec.log_path, e)

    # -- main loop ---------------------------------------------------------- #
    def run(self, max_reconnects: int | None = None) -> None:
        """Run the tick thread and the SSE reconnect loop until stopped.

        A daemon thread ticks the controller every ``interval`` seconds; the
        main thread consumes the SSE stream inside a capped-backoff reconnect
        loop, routing each event to :meth:`_handle_event`. Mirrors
        :meth:`hueman.watch.MotionController.run`'s stop/lock/backoff discipline.

        Args:
            max_reconnects: Stop after this many connect attempts (for tests);
                ``None`` runs until :meth:`stop` or a fatal auth error.
        """
        self._stop_event.clear()
        self._clear_stale_security_files(self._clock())
        _LOG.info(
            "circadian daemon driving %r (grouped_light %s); interval=%.0fs transition=%.0fs hand_off=%02d:%02d",
            self._spec.zone,
            self._rid,
            self._spec.interval_ms / 1000,
            self._spec.transition_ms / 1000,
            self._spec.hand_off_min // 60,
            self._spec.hand_off_min % 60,
        )
        interval_s = self._spec.interval_ms / 1000.0
        ticker = threading.Thread(target=self._tick_loop, args=(interval_s,), daemon=True)
        ticker.start()
        if self._bias is not None and self._bias.probe_enabled:
            probe_iv = self._bias.probe_interval_ms / 1000.0
            self._bias_probe_thread = threading.Thread(
                target=self._probe_loop, args=(probe_iv,), daemon=True
            )
            self._bias_probe_thread.start()
        # Fast control-file bias poll — runs whenever file triggers exist, even
        # with the network probe disabled, so an external signaller (e.g. HA's
        # webOS shell_command) engages bias in seconds, not on the 60s tick.
        if self._bias is not None and (self._bias.file_on or self._bias.file_off):
            file_iv = max(0.5, self._bias.probe_interval_ms / 1000.0)
            self._bias_file_thread = threading.Thread(
                target=self._bias_file_loop, args=(file_iv,), daemon=True
            )
            self._bias_file_thread.start()
        if self._security is not None:
            sec_poll = self._security.poll_interval_ms / 1000.0
            self._security_thread = threading.Thread(
                target=self._security_loop, args=(sec_poll,), daemon=True
            )
            self._security_thread.start()
        connects = 0
        backoff_initial = self._spec.retry_on_error_ms / 1000.0
        backoff_max = self._spec.sse_backoff_max_ms / 1000.0
        backoff = backoff_initial
        try:
            while not self._stop_event.is_set():
                if connects > 0:
                    self._sleep(backoff + random.uniform(0.0, backoff * 0.25))  # jitter
                connects += 1
                try:
                    for event in self._stream_factory().events():
                        backoff = backoff_initial  # receiving data -> healthy link
                        if self._stop_event.is_set():
                            break
                        self._safe_handle_event(event, self._clock())
                except AuthError:
                    raise  # a rejected key will not fix itself; fail loudly
                except (BridgeError, requests.RequestException, OSError) as e:
                    _LOG.warning("event stream error: %s; reconnecting", e)
                    backoff = min(backoff * 2, backoff_max)
                if max_reconnects is not None and connects >= max_reconnects:
                    break
        finally:
            self._stop_event.set()
            ticker.join(timeout=interval_s + 1.0)
            if self._bias_probe_thread is not None:
                self._bias_probe_thread.join(timeout=2.0)
            if self._bias_file_thread is not None:
                self._bias_file_thread.join(timeout=2.0)
            if self._security_thread is not None:
                self._security_thread.join(timeout=2.0)

    def stop(self) -> None:
        """Signal the run loop and tick thread to exit."""
        self._stop_event.set()

    def _tick_loop(self, interval_s: float) -> None:
        """Tick immediately, then every ``interval_s`` seconds, until stopped."""
        while not self._stop_event.is_set():
            try:
                self._tick_once(self._clock())
            except Exception as e:  # a tick hiccup must not kill the thread
                _LOG.warning("tick error: %s", e)
            self._stop_event.wait(interval_s)

    # -- tick --------------------------------------------------------------- #
    def _tick_once(self, now: float) -> None:
        """Decide and apply this tick's action, then poll the control file.

        The single synchronous unit of work the tick thread repeats; tests call
        it directly. Holds the lock for the whole tick so the controller mode and
        echo buffer stay consistent with a concurrent SSE event.
        """
        if self._security_active:
            return  # security mode owns the lights; skip the normal circadian/bias tick
        with self._lock:
            # Judge any settled-but-unjudged value BEFORE deciding this tick, so
            # a manual change suspends us even if the bridge never re-emitted it
            # (the SSE path alone races this tick's write). Guarded past our own
            # last fade: tick-side silence during a commanded fade means "no
            # event yet", not "value held", and judging a stale mid-fade sample
            # would false-suspend (the post-security-restore lesson, 2026-07-03).
            if now >= self._cmd_fade_until + self._settle_window_s:
                self._classify_settled_locked(now)
            action = self._controller.tick(now)
            if isinstance(action, DriveTo):
                if (
                    self._obs_brightness is not None
                    and not self._obs_classified
                    and abs(self._obs_brightness - action.brightness) > self._override_band
                ):
                    # The zone sits far from target on a value nobody has judged
                    # yet (likely a human mid-adjustment). Never write over it:
                    # skip this tick, let the SSE path or a later tick classify.
                    # Skipping also leaves _cmd_fade_until untouched, so the
                    # judgment above can run next tick even with no new events.
                    _LOG.info(
                        "deferring drive: zone at %.1f%% (target %.1f%%) not yet judged",
                        self._obs_brightness,
                        action.brightness,
                    )
                else:
                    _LOG.info(
                        "drive %r -> %.0f%% / %d mirek (%.0fs fade)",
                        self._spec.zone,
                        action.brightness,
                        action.mirek,
                        action.transition_ms / 1000,
                    )
                    target = TargetState(
                        on=True, brightness=action.brightness, mirek=action.mirek, hex=None
                    )
                    # Record the commanded target only once the bridge accepts
                    # it, so the settle detector can tell our own fade apart
                    # from a human dim — a target the zone never received must
                    # not suspend us later.
                    if self._write(target, action.transition_ms):
                        self._cmd_brightness = action.brightness
                        self._cmd_on = True
                        self._cmd_fade_until = now + action.transition_ms / 1000.0
            elif isinstance(action, FadeOff):
                _LOG.info(
                    "hand-off: fading %r off over %.0fs (night-idle until next window)",
                    self._spec.zone,
                    action.transition_ms / 1000,
                )
                # Mark our own fade-off (so the ensuing on:false event is read as
                # ours, not a human turning the zone off) only if it was accepted.
                if self._write(TargetState.off(), action.transition_ms):
                    self._cmd_on = False
                    self._cmd_brightness = 0.0
                    self._cmd_fade_until = now + action.transition_ms / 1000.0
            elif isinstance(action, Hold):
                _LOG.debug("hold: %s", action.reason)
            self._poll_control_file(now)
            if self._bias is not None:
                self._apply_bias(now)
            if self._rhythm is not None:
                try:
                    self._rhythm_tick(now)
                except Exception as e:  # inference must never break the drive loop
                    _LOG.warning("rhythm tick error: %s", e)

    def _apply_bias(self, now: float) -> None:
        """Drive each bias light per :func:`bias_actions` for this tick.

        Independent of the main set: ``in_window``/``curve`` come straight from the
        controller's public accessors (not its mode), so a manual override of the
        *main* zone never freezes the viewing lights. ``tv_on`` is the OR of the
        enabled trigger sources.

        A committed TV flip is an *edge*: every light gets the short
        ``bias.transition`` fade, and the flip is logged. The edge is latched
        (``_bias_last_applied_on``) once every write has either landed or hit an
        *unreachable* device: a *transient* bridge rejection (``FAILED``) leaves
        it unlatched so the next apply (probe tick or curve tick) retries,
        whereas an unplugged bulb (``UNREACHABLE``) must not block the latch —
        it will fail every tick forever, and refusing to latch turns each poll
        into a fresh "edge" that re-fires the whole viewing set (a runaway loop
        seen live 2026-07-25 when a housekeeper unplugged a bias light). Non-edge
        ``BiasOff`` writes are suppressed once the off has landed — re-PUTing off
        to the whole viewing set every tick, all night, is pure queue pressure.
        """
        bias = self._bias
        if bias is None:
            return  # callers guard on spec.bias; narrowing for the type checker
        in_window = self._controller.in_window(now)
        curve = self._controller.drive_to(now) if in_window else None
        tv_on = self._bias_aggregator.tv_on(now)
        edge = tv_on != self._bias_last_applied_on
        if edge:
            self._bias_off_written.clear()   # re-arm off writes for the new state
            _LOG.info(
                "bias: TV %s (%s) -> %d lights, %.1fs edge fade",
                "on" if tv_on else "off",
                self._bias_aggregator.last_source or "startup",
                len(bias.lights),
                bias.transition_ms / 1000,
            )
        latch = True   # clears only on a *transient* failure worth retrying
        for action in bias_actions(
            bias, tv_on=tv_on, in_window=in_window, curve=curve,
            transition_ms=self._spec.transition_ms, fade_off_ms=self._spec.fade_off_ms,
            edge=edge,
        ):
            rid = self._bias_rids.get(action.light)
            if rid is None:
                continue  # name not resolved to a bridge light (e.g. for_test)
            if isinstance(action, BiasHold):
                look = action.look
                target = TargetState(
                    on=True, brightness=look.brightness,
                    mirek=(look.color.mirek if look.color else None),
                    hex=(look.color.hex if look.color else None),
                )
                result = self._write_light(rid, target, action.transition_ms)
                self._bias_off_written.discard(rid)
            elif isinstance(action, BiasDrive):
                result = self._write_light(
                    rid, TargetState(on=True, brightness=action.brightness,
                                     mirek=action.mirek, hex=None), action.transition_ms)
                self._bias_off_written.discard(rid)
            else:  # BiasOff
                if rid in self._bias_off_written:
                    continue  # already off; nothing to re-write until an edge
                result = self._write_light(rid, TargetState.off(), action.transition_ms)
                if result is _BiasWrite.OK:
                    self._bias_off_written.add(rid)
            if result is _BiasWrite.FAILED:
                latch = False   # transient reject — retry on the next apply
        if latch:
            # Latch once every write landed or hit a dead device: an unreachable
            # bulb must not perpetually re-arm the edge (see the docstring).
            self._bias_last_applied_on = tv_on

    def _write_light(self, rid: str, target: TargetState, transition_ms: int) -> _BiasWrite:
        """PUT a single ``light`` body; a failure is logged and classified.

        Returns :class:`_BiasWrite` so :meth:`_apply_bias` can tell three cases
        apart: a clean write (``OK``), a *transient* bridge rejection to retry
        (``FAILED``, e.g. 'command queue is full'), and an *unreachable* device
        (``UNREACHABLE``, a bulb that is unplugged/off-at-the-wall) which must
        not stall the edge latch — the bridge will report it every tick forever.
        """
        body = LightCommand.build(target, transition_ms)
        try:
            self._client.update_resource("light", rid, body)
            return _BiasWrite.OK
        except BridgeError as e:
            _LOG.warning("bias write to %s failed (%s); skipping", rid, e)
            return _BiasWrite.UNREACHABLE if e.unreachable else _BiasWrite.FAILED

    def _write(self, target: TargetState, transition_ms: int) -> bool:
        """Serialise ``target`` and PUT it.

        A write failure is logged and swallowed — the next tick will try again.
        Returns True on success so the tick only latches ``_cmd_*`` (the settle
        detector's commanded target) for writes the bridge actually accepted; a
        target the zone never received must not be compared against later.
        """
        body = GroupedLightCommand.build(target, transition_ms)
        try:
            self._client.update_resource("grouped_light", self._rid, body)
            return True
        except BridgeError as e:
            # A transient write failure must not kill the daemon; log and retry next tick.
            # Auth asymmetry: a revoked key surfaces here as a swallowed BridgeError on
            # writes, but the SSE loop's explicit 401/403 → AuthError (re-raised in run())
            # is what brings the process down — auth loss is eventually fatal, not silently
            # ignored forever. Docker --restart always then surfaces it to the operator.
            _LOG.warning("write to %s failed (%s); skipping", self._rid, e)
            return False

    def _poll_control_file(self, now: float) -> None:
        """Resume the daemon if the external control file is present, then remove it.

        Lets an out-of-band trigger (a button automation, a cron job) clear a
        manual-override suspension by touching ``spec.control_file``.
        """
        path = self._spec.control_file
        try:
            if os.path.exists(path):
                _LOG.info("resume via control-file -> driving")
                self._controller.on_resume(now)
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass  # raced with another remover; the resume already happened
        except OSError as e:  # pragma: no cover - filesystem-dependent
            _LOG.warning("control-file poll failed for %s: %s", path, e)
        self._poll_bias_files(now)

    def _poll_bias_files(self, now: float) -> None:
        """Control-file bias source: an ``on_file``/``off_file`` flips ``tv_on``.

        Lets a sibling process (e.g. a Home Assistant ``shell_command``) signal
        the TV state by touching a file on a shared mount. Each flag is consumed
        (removed) once read, so it is edge-triggered and idempotent.
        """
        bias = self._bias
        if bias is None:
            return
        if bias.file_on:
            self._consume_flag(bias.file_on, lambda: self._bias_aggregator.on(now, "file"), "bias ON")
        if bias.file_off:
            self._consume_flag(bias.file_off, lambda: self._bias_aggregator.off(now, "file"), "bias OFF")

    # -- rhythm engine (observe stage) --------------------------------------- #
    def _load_anchor_store(self) -> AnchorStore:
        """Load learned anchors from the state file; missing/corrupt = empty.

        An unreadable store must never stop the daemon: learning restarts and
        the engine falls back to config defaults (the spec's safety rail).
        """
        assert self._rhythm is not None  # only called when rhythm is configured
        try:
            with open(self._rhythm.state_file) as fh:
                return AnchorStore.from_json(json.load(fh))
        except (OSError, ValueError):
            return AnchorStore()

    def _read_alarm_epoch(self) -> float | None:
        """Read the next-alarm signal file; garbage/0/missing/unset -> None."""
        rhythm = self._rhythm
        if rhythm is None or not rhythm.signals.next_alarm_file:
            return None
        try:
            with open(rhythm.signals.next_alarm_file) as fh:
                raw = fh.read().strip()
            value = float(raw)
            return value if value > 0 else None
        except (OSError, ValueError):
            return None

    def _phone_charging(self) -> bool:
        """The charging signal file's existence means the phone is charging."""
        rhythm = self._rhythm
        if rhythm is None or not rhythm.signals.charging_file:
            return False
        try:
            return os.path.exists(rhythm.signals.charging_file)
        except OSError:  # pragma: no cover - filesystem-dependent
            return False

    def _rhythm_note_manual(self, now: float) -> None:
        """Feed a detected manual override into presence as human evidence."""
        if self._presence is None:
            return
        judgment = self._presence.feed(
            ActivityEvent(room="", kind="light_change", ts=now))
        _LOG.info("rhythm: manual light change -> human evidence (%s)", judgment.rule)

    def _rhythm_tick(self, now: float) -> None:
        """One observe-stage inference tick: decide, log evidence, persist.

        Never writes to the bridge; a failure here must not disturb the
        circadian tick, so callers wrap it (see :meth:`_tick_once`).
        """
        engine, presence, rhythm = self._rhythm_engine, self._presence, self._rhythm
        if engine is None or presence is None or rhythm is None:
            return
        assert self._rhythm_tz is not None  # set together with the engine in _setup
        # Sunset minute via the solar calculator (same source the curve uses);
        # a polar infinite sunset falls back to 20:00.
        local_date = _dt.datetime.fromtimestamp(now, tz=self._rhythm_tz).date()
        sun = self._solar.sun_times(local_date)
        sunset_min = float(sun.sunset_min) if math.isfinite(sun.sunset_min) else 20 * 60.0
        signals = SignalState(
            next_alarm_epoch=self._read_alarm_epoch(),
            phone_charging=self._phone_charging(),
            tv_on=bool(self._bias_aggregator.tv_on(now)) if self._bias is not None else False,
            zone_on=self._cmd_on,
            sunset_min=sunset_min,
        )
        decision = engine.tick(now, presence.summary(now), signals)
        if decision.changed:
            _LOG.info("rhythm: phase -> %s (%s) evidence=%s",
                      decision.phase, decision.reason, json.dumps(decision.evidence))
            self._persist_rhythm_state(now)

    def _persist_rhythm_state(self, now: float) -> None:
        """Atomically write anchors + snapshot to the state file."""
        engine, rhythm = self._rhythm_engine, self._rhythm
        if engine is None or rhythm is None:
            return
        doc: dict[str, object] = {"version": 1, "snapshot": engine.snapshot(now)}
        doc.update(engine.store.to_json())  # {"anchors": ...} via the engine's store property
        try:
            tmp = rhythm.state_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(doc, fh, indent=2)
            os.replace(tmp, rhythm.state_file)
        except OSError as e:  # pragma: no cover - filesystem-dependent
            _LOG.warning("rhythm state write failed for %s: %s", rhythm.state_file, e)

    def _consume_flag(self, path: str, action: Callable[[], None], label: str) -> None:
        """If ``path`` exists, run ``action`` and remove it (best-effort)."""
        try:
            if os.path.exists(path):
                _LOG.info("%s via control-file %s", label, path)
                action()
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass  # raced with another remover
        except OSError as e:  # pragma: no cover - filesystem-dependent
            _LOG.warning("bias control-file poll failed for %s: %s", path, e)

    # -- security mode ------------------------------------------------------ #
    def _poll_security_files(self, now: float) -> None:
        """control-file security source: an on_file/off_file flips the armed signal."""
        sec = self._security
        if sec is None:
            return
        if sec.file_on:
            self._consume_flag(
                sec.file_on, lambda: self._security_aggregator.on(now, "file"), "security ARM")
        if sec.file_off:
            self._consume_flag(
                sec.file_off, lambda: self._security_aggregator.off(now, "file"), "security DISARM")

    def _run_security_show(self, start: float) -> None:
        """Drive the escalating show until disarm or the max-duration cap, then revert.

        Owns the lights for its duration: the normal tick is a no-op while
        ``_security_active`` (see :meth:`_tick_once`). On exit it emits the
        ``clear`` cue, clears the control files, and runs one normal tick so the
        lights snap back to their current circadian/bias look.
        """
        sec, controller = self._security, self._security_controller
        if sec is None or controller is None:
            return
        _LOG.info("SECURITY MODE engaged across %s", ", ".join(sec.groups))
        self._security_active = True
        self._security_last_phase = None
        frame_interval_s = sec.frame_interval_ms / 1000.0
        frame_index = 0
        attempted = dropped = 0
        disarmed = False               # True only when an off() edge ended the show
        # Frame writes run CONCURRENTLY: at a 175ms frame budget, serial
        # ~50-150ms HTTPS round-trips would overrun every frame and stretch the
        # whole show's cadence (observed live as 'seconds between changes').
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="sec-frame") as pool:
            try:
                while not self._stop_event.is_set():
                    now = self._clock()
                    elapsed_ms = (now - start) * 1000.0
                    with self._lock:
                        self._poll_security_files(now)        # disarm-by-file mid-show
                        armed = self._security_aggregator.active(now)
                    if not armed:
                        disarmed = True
                        break
                    if controller.is_expired(elapsed_ms):
                        _LOG.info("security max-duration reached -> standing down")
                        break
                    frame = controller.frame_at(elapsed_ms, frame_index)
                    if frame.phase != self._security_last_phase:
                        self._emit_cue(frame.phase)
                        self._security_last_phase = frame.phase
                    futures = []
                    for ft in frame.targets:
                        rid: str | None
                        if ft.kind == "light":
                            rid, rtype = ft.name, "light"     # chaos units ARE light rids
                        else:
                            rid, rtype = self._security_rids.get(ft.name), "grouped_light"
                        if rid is not None:
                            # Alert breathes (fade over one frame); chaos is hard
                            # cuts — without an explicit duration the bulbs'
                            # default ~400ms fade turns chaos into a gradual drift.
                            transition = sec.frame_interval_ms if frame.phase == "alert" else 0
                            futures.append(pool.submit(
                                self._write_security_frame, rtype, rid, ft.target, transition))
                    for fut in futures:
                        attempted += 1
                        if not fut.result():
                            dropped += 1
                    frame_index += 1
                    self._stop_event.wait(frame_interval_s)
            finally:
                self._emit_cue("clear")
                self._reset_security_files()
                with self._lock:
                    # Consume the arm signal on EVERY exit path. A max-duration
                    # standdown never sees an off() edge, and a still-on
                    # aggregator re-engages the show on the security loop's very
                    # next poll (observed live 2026-07-03). Trade-off: an ARM
                    # that races in during teardown is consumed too — arm again.
                    # Only a disarm exit can tell a raced re-arm apart from the
                    # original never-off'd signal, so only that path logs it.
                    if disarmed and self._security_aggregator.active(self._clock()):
                        _LOG.info(
                            "security re-arm during standdown consumed; arm again to re-engage")
                    self._security_aggregator.reset(self._clock())
                self._security_active = False
                self._security_last_phase = None
                if dropped:
                    _LOG.warning(
                        "security show: bridge dropped %d/%d frame writes "
                        "(REST rate limit) — the show degrades when this grows",
                        dropped, attempted)
                _LOG.info("SECURITY MODE cleared -> resuming normal operation")
                try:
                    self._restore_after_security(self._clock())
                except Exception as e:  # pragma: no cover - best-effort restore
                    _LOG.warning("post-security restore failed: %s", e)

    def _restore_after_security(self, now: float) -> None:
        """Snap every stomped light back to its true 'normal' for this moment.

        One normal tick is NOT enough: overnight the tick is deliberately
        hands-off (night state is dark), and the bias off-suppression believes
        its pre-show bookkeeping — either way the apartment would freeze in the
        last chaos colours. So: reset the bias edge state (forcing a fresh
        edge-fade re-assert of every viewing light), run the tick (in-window it
        re-drives the main zone + bias), and out of window explicitly fade the
        security zones dark, with the main zone's off recorded as OUR OWN so
        settle-and-compare doesn't read it as a human override.
        """
        with self._lock:
            self._bias_off_written.clear()
            self._bias_last_applied_on = None        # next apply is a forced edge
            # The restore drive below is a big catch-up ramp from wherever chaos
            # left the zone. A slow segment of that ramp can hold within
            # settle_epsilon across the settle window and read as a human
            # override (observed live 2026-07-03: 'settled at 45.6% vs target
            # 99.8%'). Drop the chaos-churned observations and skip settle
            # classification until one full fade + settle window has passed.
            # Chosen trade-off: a REAL manual dim inside that grace is
            # indistinguishable from the ramp and gets re-driven once; a second
            # adjustment after the grace suspends normally.
            self._obs_brightness = None
            self._obs_classified = False
            self._classify_grace_until = now + (
                self._spec.transition_ms + self._spec.settle_window_ms) / 1000.0
        self._tick_once(now)
        with self._lock:
            if self._controller.in_window(now):
                return                               # the tick re-drove everything
            edge_ms = self._bias.transition_ms if self._bias is not None else 2_000
            for group, rid in self._security_rids.items():
                if group == self._spec.zone or rid == self._rid:
                    # Our own zone: mirror the FadeOff bookkeeping so the
                    # ensuing off event reads as ours, not a human override.
                    self._cmd_on = False
                    self._cmd_brightness = 0.0
                self._write_security_frame(
                    "grouped_light", rid, TargetState.off(), edge_ms)
                _LOG.info("security restore: %r -> off (night idle)", group)

    def _write_security_frame(self, rtype: str, rid: str, target: TargetState,
                              transition_ms: int) -> bool:
        """PUT one security frame body; transient errors are swallowed.

        Returns True on success so the show loop can count drops — silent
        rate-limit losses are how chaos degrades into sparse pops.
        """
        body = GroupedLightCommand.build(target, transition_ms)
        try:
            self._client.update_resource(rtype, rid, body)
            return True
        except BridgeError as e:
            _LOG.debug("security frame write to %s failed (%s); skipping", rid, e)
            return False

    def _emit_cue(self, name: str) -> None:
        """Write the phase cue for an external (HA -> Sonos) audio adapter."""
        sec = self._security
        if sec is None:
            return
        if sec.cue_file:
            try:
                with open(sec.cue_file, "w") as fh:
                    fh.write(name + "\n")
            except OSError as e:  # pragma: no cover - filesystem-dependent
                _LOG.warning("security cue-file write failed for %s: %s", sec.cue_file, e)
        if sec.cue_webhook:
            try:
                requests.post(sec.cue_webhook, json={"cue": name}, timeout=2.0)
            except requests.RequestException as e:  # pragma: no cover - network-dependent
                _LOG.warning("security cue webhook failed (%s)", e)

    def _reset_security_files(self) -> None:
        """Best-effort clear of both control files so a stale flag can't re-fire."""
        sec = self._security
        if sec is None:
            return
        for path in (sec.file_on, sec.file_off):
            if not path:
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as e:  # pragma: no cover - filesystem-dependent
                _LOG.warning("could not remove security control-file %s: %s", path, e)

    def _security_loop(self, poll_s: float) -> None:
        """Poll security triggers; run the show while armed. Runs in its own thread."""
        while not self._stop_event.is_set():
            try:
                now = self._clock()
                with self._lock:
                    self._poll_security_files(now)
                    armed = self._security_aggregator.active(now)
                if armed and not self._security_active:
                    self._run_security_show(now)
            except Exception as e:  # a security hiccup must not kill the thread
                _LOG.warning("security loop error: %s", e)
            self._stop_event.wait(poll_s)

    def _clear_stale_security_files(self, now: float) -> None:
        """Remove an on/off control-file older than max_duration so a restart can't
        re-fire a long-abandoned panic. Called once at startup, before threads run."""
        sec = self._security
        if sec is None:
            return
        max_age_s = sec.max_duration_ms / 1000.0
        for path in (sec.file_on, sec.file_off):
            if not path:
                continue
            try:
                if os.path.exists(path) and (now - os.path.getmtime(path)) > max_age_s:
                    os.unlink(path)
                    _LOG.info("cleared stale security control-file %s at startup", path)
            except OSError as e:  # pragma: no cover - filesystem-dependent
                _LOG.warning("stale security-file check failed for %s: %s", path, e)

    # -- bias TV-state probe (optional source) ------------------------------ #
    def _probe_reachable(self) -> bool:
        """Return True if the configured TV host answers (TCP connect or ICMP ping)."""
        bias = self._bias
        if bias is None or not bias.probe_host:
            return False
        if bias.probe_mode == "tcp":
            try:
                with socket.create_connection((bias.probe_host, bias.probe_port), timeout=2.0):
                    return True
            except OSError:
                return False
        try:  # icmp
            done = subprocess.run(
                ["ping", "-c", "1", "-W", "1", bias.probe_host],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return done.returncode == 0
        except OSError:
            return False

    def _probe_tick(self, now: float) -> None:
        """One probe poll: feed reachability into the bias aggregator, then apply
        bias immediately if that flipped the committed TV state.

        Applying here (on the ~5s probe cadence) rather than only on the next
        ``interval`` curve tick is what keeps the bias hold responsive — the TV
        turning on/off changes the lights in seconds, not up to a minute.
        """
        if self._security_active:
            return  # security owns the lights; the probe re-reads on the next tick after the show
        if self._bias is None or not self._bias.probe_enabled:
            return
        reachable = self._probe_reachable()   # network probe stays OUTSIDE the lock
        with self._lock:
            # The aggregator is a shared read-modify-write (raw/committed/debounce
            # clock) also touched by the tick and SSE threads under this lock; an
            # unlocked mutation here could commit a flip with the debounce skipped.
            if reachable:
                self._bias_aggregator.on(now, "probe")
            else:
                self._bias_aggregator.off(now, "probe")
            self._apply_bias_if_changed_locked(now)

    def _apply_bias_if_changed(self, now: float) -> None:
        """Lock-taking wrapper around :meth:`_apply_bias_if_changed_locked`."""
        with self._lock:
            self._apply_bias_if_changed_locked(now)

    def _apply_bias_if_changed_locked(self, now: float) -> None:
        """Apply bias now iff the committed ``tv_on`` differs from what we last
        drove. Caller must hold ``self._lock`` (the lock is not reentrant). A
        no-op when unchanged, so a steady TV state does not flood the bridge
        with redundant per-light writes.
        """
        if self._bias is None:
            return
        if self._bias_aggregator.tv_on(now) != self._bias_last_applied_on:
            self._apply_bias(now)

    def _bias_file_tick(self, now: float) -> None:
        """Fast path for the control-file bias source: consume the on/off flags
        and apply bias the moment ``tv_on`` flips.

        This runs on its own quick cadence (independent of the network probe),
        so a sibling signaller — e.g. a Home Assistant ``shell_command`` reacting
        to real webOS power state — engages/releases bias in seconds, not only on
        the up-to-60s curve tick. A no-op while a security show owns the lights
        (the flag is left for after the show).
        """
        if self._security_active:
            return
        bias = self._bias
        if bias is None or not (bias.file_on or bias.file_off):
            return
        with self._lock:
            self._poll_bias_files(now)
            self._apply_bias_if_changed_locked(now)

    def _bias_file_loop(self, interval_s: float) -> None:
        """Poll the bias control-files every ``interval_s`` until stopped."""
        while not self._stop_event.is_set():
            try:
                self._bias_file_tick(self._clock())
            except Exception as e:  # a poll hiccup must not kill the thread
                _LOG.warning("bias file poll error: %s", e)
            self._stop_event.wait(interval_s)

    def _probe_loop(self, interval_s: float) -> None:
        """Poll the TV reachability probe every ``interval_s`` until stopped."""
        while not self._stop_event.is_set():
            try:
                self._probe_tick(self._clock())
            except Exception as e:  # a probe hiccup must not kill the thread
                _LOG.warning("probe error: %s", e)
            self._stop_event.wait(interval_s)

    # -- event handling ----------------------------------------------------- #
    def _safe_handle_event(self, event: BridgeEvent, now: float) -> None:
        """Handle one event, isolating failures so a bad frame is non-fatal."""
        try:
            self._handle_event(event, now)
        except Exception as e:  # a single malformed/odd event must not stop the loop
            _LOG.warning("skipping unprocessable %s event %s: %s", event.rtype, event.rid, e)

    def _handle_event(self, event: BridgeEvent, now: float) -> None:
        """Route one bridge event into the controller (override / resume).

        Decision contract (settle-and-compare; see the module docstring):

        * ``scene``/``button`` matching the resolved resume trigger -> resume.
        * Otherwise only this zone's ``grouped_light`` events matter.
        * ``on:false`` while the daemon expects the zone *on* (``_cmd_on``) -> a
          human turned it off -> suspend. ``on:false`` after our own fade-off
          (``_cmd_on`` already False) is ours and ignored.
        * ``on:true`` after an observed ``on:false`` while *suspended* re-engages
          the daemon when ``resume_on_power_cycle`` is set (a power cycle).
        * A ``dimming`` brightness is judged only once it has *held* (within
          ``settle_epsilon``) for ``settle_window``: a value still moving is a
          fade and is not classified; a settled value beyond ``override_band`` of
          the commanded target is a human override -> suspend; within the band it
          is our own look -> no-op.
        """
        with self._lock:
            if event.rtype in ("scene", "button"):
                # Bias TV-on/off triggers (SSE source). A scene/button may be both
                # a bias trigger and the resume trigger; check all, then return.
                if self._bias_sse_on_rid is not None and event.rid == self._bias_sse_on_rid:
                    _LOG.info("bias trigger ON (%s)", event.rid)
                    self._bias_aggregator.on(now, "sse")
                    self._apply_bias_if_changed_locked(now)   # same-event apply, like the probe
                if self._bias_sse_off_rid is not None and event.rid == self._bias_sse_off_rid:
                    _LOG.info("bias trigger OFF (%s)", event.rid)
                    self._bias_aggregator.off(now, "sse")
                    self._apply_bias_if_changed_locked(now)
                if self._security is not None:
                    if self._security_sse_on_rid is not None and event.rid == self._security_sse_on_rid:
                        _LOG.info("security ARM via SSE (%s)", event.rid)
                        self._security_aggregator.on(now, "sse")
                    if self._security_sse_off_rid is not None and event.rid == self._security_sse_off_rid:
                        _LOG.info("security DISARM via SSE (%s)", event.rid)
                        self._security_aggregator.off(now, "sse")
                if self._resume_trigger_rid is not None and event.rid == self._resume_trigger_rid:
                    _LOG.info("resume trigger %s -> resumed", event.rid)
                    self._controller.on_resume(now)
                return
            if (self._presence is not None
                    and event.rtype in ("convenience_area_motion", "security_area_motion")):
                room = self._rhythm_motion_rooms.get(event.rid)
                if room is not None and event.data.get("motion", {}).get("motion"):
                    judgment = self._presence.feed(
                        ActivityEvent(room=room, kind="motion", ts=now))
                    _LOG.debug("rhythm: motion in %r -> human=%s (%s)",
                               room, judgment.human, judgment.rule)
                return
            if event.rtype != "grouped_light" or event.rid != self._rid:
                return
            on_state = event.data.get("on", {}).get("on")
            bri = event.data.get("dimming", {}).get("brightness")
            _LOG.debug("event grouped_light %s on=%s dimming=%s", event.rid, on_state, bri)

            # -- on/off + power-cycle -------------------------------------- #
            if on_state is not None:
                if on_state is False:
                    self._last_on = False
                    if self._cmd_on and self._controller.mode != CircadianController.SUSPENDED:
                        _LOG.info("zone turned off -> suspended")
                        self._controller.on_external_change(now)
                        self._rhythm_note_manual(now)
                    # else: cmd_on already False -> our own fade-off, ignore.
                else:  # on_state is True
                    if (
                        self._last_on is False
                        and self._controller.mode == CircadianController.SUSPENDED
                        and self._spec.resume_on_power_cycle
                    ):
                        _LOG.info("power-cycle (off->on) -> resumed")
                        self._controller.on_resume(now)
                    self._last_on = True

            # -- brightness settle-and-compare ----------------------------- #
            # Skip on an explicit off (the value is the fade-to-0, not a dim).
            if bri is None or on_state is False:
                return
            if self._obs_brightness is None or abs(bri - self._obs_brightness) > self._settle_epsilon:
                # value moved (still fading) -> reset the settle window, do NOT classify.
                self._obs_brightness = bri
                self._obs_since = now
                self._obs_classified = False
                return
            # holding within epsilon of the last observed value
            self._classify_settled_locked(now)

    def _classify_settled_locked(self, now: float) -> None:
        """Judge a settled-but-unjudged brightness against the commanded target.

        Caller holds the lock. A no-op unless the observed value has *held*
        (no move within ``settle_epsilon``) for at least ``settle_window`` and
        has not been classified yet. Settled beyond ``override_band`` of the
        commanded target is a human override -> suspend; within the band it is
        our own look -> no-op.

        Called from two places: the SSE path when the bridge re-emits a held
        value, and the tick (guarded by ``_cmd_fade_until``) so a judgment
        never waits on the bridge's re-emission cadence — without the tick-side
        call, a tick landing before the re-emission would stomp a manual change
        that was never judged.
        """
        if self._obs_brightness is None or self._obs_classified:
            return
        if now - self._obs_since < self._settle_window_s:
            return  # not stable long enough yet
        # SETTLED on a single value for >= settle_window.
        self._obs_classified = True
        bri = self._obs_brightness
        if now < self._classify_grace_until:
            _LOG.debug(
                "settled at %.1f%% within post-restore grace -> not classified", bri)
            return
        # TODO(housekeeper-resilience): `bri` is the *grouped* brightness, an
        # aggregate over the zone's members. An unreachable Circadian Core
        # member (a bulb a housekeeper unplugged) can hold that aggregate
        # off-target and trip a false "manual override -> suspend" — the core
        # twin of the bias-latch cascade fixed in #8. Audit: fold
        # zigbee_connectivity into this comparison so a connectivity_issue
        # member can't suspend the day drive. (Not yet observed live; the
        # 2026-07-25 core suspend was a genuine manual zone-off.)
        if (
            self._controller.mode != CircadianController.SUSPENDED
            and self._cmd_on
            and self._cmd_brightness is not None
            and abs(bri - self._cmd_brightness) > self._override_band
        ):
            _LOG.info(
                "manual override: settled at %.1f%% vs target %.1f%% -> suspended",
                bri,
                self._cmd_brightness,
            )
            self._controller.on_external_change(now)
            self._rhythm_note_manual(now)
        else:
            target_str = "?" if self._cmd_brightness is None else f"{self._cmd_brightness:.1f}%"
            _LOG.debug(
                "settled at %.1f%% (within band of target %s) -> self", bri, target_str
            )
