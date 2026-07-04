"""LEGACY runtime that enforces motion policies from the bridge event stream.

.. warning::
   **Not deployed, and known-inadequate against current bridge behaviour.** Two
   findings from the live deployment supersede this module's design: (1) the
   bridge periodically re-emits settled ``grouped_light`` values long after the
   4 s echo TTL, which this module's echo-buffer heuristic misreads as manual
   overrides (``circadian_daemon``'s settle-and-compare detection is the current
   answer); (2) it only understands legacy PIR ``motion`` sensors -- a Bridge Pro
   using MotionAware (``*_area_motion`` services) has none, so ``_build`` cannot
   even construct a controller there. Kept because the engine split is sound and
   tested; fix both before ever deploying it.

``plan``/``apply`` provision the declarative surface; this module is the live
controller. It subscribes to the CLIP v2 Server-Sent Events stream
(``/eventstream/clip/v2``), feeds motion, light-level and manual-control events
into the per-area :class:`~hueman.engine.PolicyEngine` instances, and writes the
resulting commands back to the relevant ``grouped_light`` services.

Manual-override detection is a trust heuristic: every command we send is
recorded in a short-lived echo buffer, so when the bridge reports a
``grouped_light`` change we can tell our own write apart from a human reaching
for a switch or recalling a scene. An unrecognised change pauses the area's
automation for the policy's override window.

This is the one module that genuinely needs the hardware to exercise end to end,
so its decision logic is delegated to the pure, tested engine and its
serialisation to the pure, tested :mod:`hueman.payload`; what remains here is
event plumbing.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import requests

from .client import HueClient
from .config import Config
from .engine import Action, PolicyEngine
from .errors import AuthError, BridgeError
from .payload import GroupedLightCommand
from .state import BridgeState

_LOG = logging.getLogger("hueman.watch")

#: How long a command we issued stays in the echo buffer before a matching
#: bridge event is assumed to be a human action instead of our own write.
_ECHO_TTL_SECONDS = 4.0

#: SSE connection timeouts: (connect, read). A finite read timeout lets a dead
#: but unclosed socket surface as an error the reconnect loop can recover from,
#: instead of wedging the controller forever.
_SSE_CONNECT_TIMEOUT = 5.0
_SSE_READ_TIMEOUT = 90.0


@dataclass(frozen=True)
class BridgeEvent:
    """A single decoded resource update from the event stream.

    Attributes:
        rtype: The resource type, for example ``"motion"`` or ``"grouped_light"``.
        rid: The resource id the update concerns.
        data: The raw update payload for that resource.
    """

    rtype: str
    rid: str
    data: dict[str, Any]


class HueEventStream:
    """Yields decoded :class:`BridgeEvent` values from the CLIP SSE endpoint.

    Args:
        client: An authenticated bridge client (used for host, key and TLS).
    """

    def __init__(self, client: HueClient) -> None:
        """Store the bridge client whose session and key the stream reuses."""
        self._client = client

    def events(self) -> Iterator[BridgeEvent]:
        """Open the stream and yield events until the connection drops.

        Yields:
            One :class:`BridgeEvent` per resource update in each SSE message.
        """
        url = f"https://{self._client.bridge.host}/eventstream/clip/v2"
        headers = {
            "Accept": "text/event-stream",
            "hue-application-key": self._client.bridge.application_key or "",
        }
        session = self._client._session  # reuse the configured (pinned) TLS session
        with session.get(
            url,
            headers=headers,
            stream=True,
            timeout=(_SSE_CONNECT_TIMEOUT, _SSE_READ_TIMEOUT),
        ) as response:
            if response.status_code in (401, 403):
                raise AuthError(
                    f"bridge rejected the application key (HTTP {response.status_code}); "
                    "re-run 'hueman auth'"
                )
            response.raise_for_status()  # other 4xx/5xx -> retryable HTTPError
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                yield from self._decode(line[len("data:"):].strip())

    @staticmethod
    def _decode(payload: str) -> Iterator[BridgeEvent]:
        """Decode one SSE ``data:`` payload into individual events."""
        try:
            messages = json.loads(payload)
        except json.JSONDecodeError:
            return
        for message in messages:
            for item in message.get("data", []):
                rtype = item.get("type")
                rid = item.get("id")
                if rtype and rid:
                    yield BridgeEvent(rtype=rtype, rid=rid, data=item)


@dataclass
class _EchoEntry:
    """A recently issued command awaiting its echo from the bridge."""

    grouped_light_rid: str
    expires_ts: float


class MotionController:
    """Owns the engines and applies their actions against the bridge.

    Args:
        client: An authenticated bridge client.
        state: A loaded :class:`~hueman.state.BridgeState`.
        config: The parsed IaC configuration.
        clock: Callable returning epoch seconds; injectable for testing.
        dry_run: When ``True``, log actions instead of writing to the bridge.
    """

    def __init__(
        self,
        client: HueClient,
        state: BridgeState,
        config: Config,
        clock: Callable[[], float] = time.time,
        dry_run: bool = False,
        *,
        stream_factory: Callable[[], "HueEventStream"] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
    ) -> None:
        """Wire up engines and routing tables; see the class docstring for args."""
        self._client = client
        self._state = state
        self._config = config
        self._clock = clock
        self._dry_run = dry_run
        self._stream_factory = stream_factory or (lambda: HueEventStream(self._client))
        self._sleep = sleep
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._engines: list[PolicyEngine] = []
        self._area_to_grouped_light: dict[str, str] = {}
        self._grouped_light_to_area: dict[str, str] = {}
        self._motion_rid_to_area: dict[str, list[str]] = {}
        self._light_level_rid_to_area: dict[str, list[str]] = {}
        self._echoes: list[_EchoEntry] = []
        # Guards engine state + the echo buffer, which the SSE loop (events) and
        # the tick thread (timers) both mutate.
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._build()

    # -- wiring ------------------------------------------------------------- #
    def _build(self) -> None:
        """Construct engines and the event-id-to-area routing tables."""
        location = (
            self._config.location.lat,
            self._config.location.lon,
            self._config.location.tz_offset_hours,
        )
        for policy in self._config.motion_policies:
            engine = PolicyEngine(policy, location, self._config.circadian)
            self._engines.append(engine)
            sensor = self._state.sensor(policy.sensor)
            for area_name in policy.areas:
                group = self._state.group(area_name)
                if group.grouped_light_rid is not None:
                    self._area_to_grouped_light[area_name] = group.grouped_light_rid
                    self._grouped_light_to_area[group.grouped_light_rid] = area_name
                if sensor.motion_rid is not None:
                    self._motion_rid_to_area.setdefault(sensor.motion_rid, []).append(area_name)
                if sensor.light_level_rid is not None:
                    self._light_level_rid_to_area.setdefault(sensor.light_level_rid, []).append(area_name)

    def _engine_for_area(self, area: str) -> PolicyEngine | None:
        """Return the engine that controls ``area``, if any."""
        for engine in self._engines:
            if area in engine.policy.areas:
                return engine
        return None

    # -- main loop ---------------------------------------------------------- #
    def run(self, tick_interval: float = 1.0, *, max_reconnects: int | None = None) -> None:
        """Converge to standby, then process events and timers until stopped.

        Resilient to stream drops: the SSE connection is consumed inside a
        reconnect loop with capped exponential backoff, re-converging to standby
        on each (re)connect. Engine timers run on an independent thread so the
        idle -> dim -> off progression fires on a real schedule rather than only
        when a bridge event happens to arrive.

        Args:
            tick_interval: Seconds between timer ticks.
            max_reconnects: Stop after this many connect attempts (for tests);
                ``None`` runs until :meth:`stop` or a fatal error.
        """
        self._stop_event.clear()
        ticker = threading.Thread(target=self._tick_loop, args=(tick_interval,), daemon=True)
        ticker.start()
        # Converge to a known state ONCE at startup. Engine phase (ACTIVE /
        # OVERRIDDEN) survives a stream drop in-process, so re-converging on
        # every reconnect would switch occupied rooms off and wipe manual
        # overrides on any transient blip.
        with self._lock:
            self._converge_standby()
        connects = 0
        backoff = self._backoff_initial
        try:
            while not self._stop_event.is_set():
                if connects > 0:
                    self._sleep(backoff + random.uniform(0.0, backoff * 0.25))  # jitter
                connects += 1
                try:
                    for event in self._stream_factory().events():
                        backoff = self._backoff_initial  # receiving data -> healthy link
                        if self._stop_event.is_set():
                            break
                        with self._lock:
                            self._safe_handle_event(event)
                except AuthError:
                    raise  # a rejected key will not fix itself; fail loudly
                except (BridgeError, requests.RequestException, OSError) as e:
                    _LOG.warning("event stream error: %s; reconnecting", e)
                    backoff = min(backoff * 2, self._backoff_max)
                if max_reconnects is not None and connects >= max_reconnects:
                    break
        finally:
            self._stop_event.set()
            ticker.join(timeout=tick_interval + 1.0)

    def stop(self) -> None:
        """Signal the run loop and tick thread to exit."""
        self._stop_event.set()

    def _tick_loop(self, tick_interval: float) -> None:
        """Fire engine timers every ``tick_interval`` seconds until stopped."""
        while not self._stop_event.wait(tick_interval):
            try:
                with self._lock:
                    self._run_ticks(self._clock())
            except Exception as e:  # a timer hiccup must not kill the thread
                _LOG.warning("tick error: %s", e)

    def _converge_standby(self) -> None:
        """Drive every area to its standby state on startup."""
        now = self._clock()
        for engine in self._engines:
            for action in engine.enter_standby_all(now):
                self._apply(action)

    def _run_ticks(self, now: float) -> None:
        """Advance every engine's timers and apply the resulting actions."""
        self._expire_echoes(now)
        for engine in self._engines:
            for action in engine.tick(now):
                self._apply(action)

    # -- event handling ----------------------------------------------------- #
    def _safe_handle_event(self, event: BridgeEvent) -> None:
        """Handle one event, isolating failures so a bad frame is non-fatal."""
        try:
            self._handle_event(event)
        except Exception as e:  # a single malformed/odd event must not stop the loop
            _LOG.warning("skipping unprocessable %s event %s: %s", event.rtype, event.rid, e)

    def _handle_event(self, event: BridgeEvent) -> None:
        """Route one bridge event to the appropriate engine handler."""
        now = self._clock()
        if event.rtype == "motion":
            self._handle_motion(event, now)
        elif event.rtype == "light_level":
            self._handle_light_level(event, now)
        elif event.rtype == "grouped_light":
            self._handle_grouped_light(event, now)

    def _handle_motion(self, event: BridgeEvent, now: float) -> None:
        """Feed a motion report to every area the sensor covers."""
        report = event.data.get("motion", {})
        if "motion" not in report and "motion_report" not in report:
            return
        present = bool(
            report.get("motion_report", {}).get("motion", report.get("motion", False))
        )
        for area in self._motion_rid_to_area.get(event.rid, []):
            engine = self._engine_for_area(area)
            if engine is not None:
                for action in engine.on_motion(area, present, now):
                    self._apply(action)

    def _handle_light_level(self, event: BridgeEvent, now: float) -> None:
        """Forward an ambient light-level reading to the brightness gate.

        The CLIP light-level service reports brightness on a logarithmic scale
        (``raw = 10000 * log10(lux) + 1``), independent of motion, so we convert
        back to lux and pass it through as a presence-less motion call that the
        engine uses only to refresh its lux gate.
        """
        report = event.data.get("light", {})
        raw = report.get("light_level_report", {}).get("light_level", report.get("light_level"))
        if raw is None:
            return
        lux = int(round(10 ** ((raw - 1) / 10000.0)))
        for area in self._light_level_rid_to_area.get(event.rid, []):
            engine = self._engine_for_area(area)
            if engine is not None:
                for action in engine.on_motion(area, False, now, lux=lux):
                    self._apply(action)

    def _handle_grouped_light(self, event: BridgeEvent, now: float) -> None:
        """Detect manual control by comparing against our recent echoes."""
        if self._consume_echo(event.rid, now):
            return  # this was our own write
        area = self._grouped_light_to_area.get(event.rid)
        if area is None:
            return
        engine = self._engine_for_area(area)
        if engine is None:
            return
        on_state = event.data.get("on", {}).get("on")
        if on_state is False:
            engine.on_manual_off(area, now)
        else:
            engine.on_manual_override(area, now)

    # -- output ------------------------------------------------------------- #
    def _apply(self, action: Action) -> None:
        """Serialise and send (or log) one engine action."""
        grouped_light_rid = self._area_to_grouped_light.get(action.area)
        if grouped_light_rid is None:
            return
        body = GroupedLightCommand.build(action.state)
        if self._dry_run:
            print(f"[dry-run] {action.area}: {action.reason} -> {body}")
            return
        self._record_echo(grouped_light_rid)
        try:
            self._client.update_resource("grouped_light", grouped_light_rid, body)
        except BridgeError as e:  # a transient write failure must not kill the controller
            _LOG.warning("write to %s failed (%s); skipping", action.area, e)

    # -- echo buffer -------------------------------------------------------- #
    def _record_echo(self, grouped_light_rid: str) -> None:
        """Remember a command so its bridge echo is not read as manual control."""
        self._echoes.append(
            _EchoEntry(grouped_light_rid=grouped_light_rid, expires_ts=self._clock() + _ECHO_TTL_SECONDS)
        )

    def _consume_echo(self, grouped_light_rid: str, now: float) -> bool:
        """Return ``True`` and drop the echo if this event was our own write."""
        self._expire_echoes(now)
        for index, entry in enumerate(self._echoes):
            if entry.grouped_light_rid == grouped_light_rid:
                del self._echoes[index]
                return True
        return False

    def _expire_echoes(self, now: float) -> None:
        """Drop echo entries that have outlived their TTL."""
        live: list[_EchoEntry] = []
        for entry in self._echoes:
            if entry.expires_ts > now:
                live.append(entry)
        self._echoes = live
