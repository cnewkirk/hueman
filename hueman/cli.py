"""Command-line interface for hueman.

Subcommands:
    auth      Pair with the bridge (press the link button) and print the key.
    validate  Parse and validate the config without touching the bridge.
    preview   Print the circadian colour curve for a date and location.
    plan      Show the changes apply would make (read-only).
    apply     Converge the bridge's declarative state to the config.
    watch     Run the live motion/timing controller.

The interface mirrors Terraform's verbs deliberately: ``validate`` -> ``plan``
-> ``apply`` is the same muscle memory, and ``plan`` never mutates anything.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys

from . import __version__
from .client import HueClient
from .config import Config, load_config
from .errors import HueIacError
from .reconcile import Change, Planner
from .state import BridgeState, Group, LightRef, MotionSensor


def _group_name(group: Group) -> str:
    """Sort key returning a group's name."""
    return group.name


def _light_name(light: LightRef) -> str:
    """Sort key returning a light's name."""
    return light.name


def _sensor_name(sensor: MotionSensor) -> str:
    """Sort key returning a sensor's name."""
    return sensor.name


class Cli:
    """Parses arguments and dispatches to subcommand handlers."""

    def __init__(self) -> None:
        self._parser = self._build_parser()

    def _build_parser(self) -> argparse.ArgumentParser:
        """Construct the argument parser and its subcommands."""
        parser = argparse.ArgumentParser(prog="hueman", description="Declarative Philips Hue management.")
        parser.add_argument("--version", action="version", version=f"hueman {__version__}")
        parser.add_argument(
            "-c", "--config", default="hue.yaml", help="path to the config file (default: hue.yaml)"
        )
        sub = parser.add_subparsers(dest="command", required=True)

        sub.add_parser("validate", help="validate the config without contacting the bridge")
        sub.add_parser("auth", help="pair with the bridge and print an application key")
        sub.add_parser("inventory", help="list the bridge's rooms, zones, lights and sensors")
        sub.add_parser("plan", help="show the changes apply would make")

        apply_parser = sub.add_parser("apply", help="converge the bridge to the config")
        apply_parser.add_argument(
            "--yes", action="store_true", help="apply without the confirmation prompt"
        )
        apply_parser.add_argument(
            "--ignore-unassigned",
            action="store_true",
            help="apply even if some lights are not assigned to any area",
        )

        preview_parser = sub.add_parser("preview", help="print the circadian curve for a day")
        preview_parser.add_argument("--date", help="ISO date (default: today)")

        watch_parser = sub.add_parser("watch", help="run the live motion controller")
        watch_parser.add_argument(
            "--dry-run", action="store_true", help="log actions instead of writing to the bridge"
        )

        circadian_parser = sub.add_parser("circadian", help="run the smooth circadian daemon")
        circ_sub = circadian_parser.add_subparsers(dest="circadian_cmd", required=True)
        circ_sub.add_parser("run", help="run the daemon (foreground; container entrypoint)")
        circ_sub.add_parser("resume", help="hand control back to a running daemon")

        security_parser = sub.add_parser("security", help="arm/disarm security mode")
        sec_sub = security_parser.add_subparsers(dest="security_cmd", required=True)
        sec_sub.add_parser("on", help="arm security mode (panic)")
        sec_sub.add_parser("off", help="disarm security mode")
        sec_sub.add_parser("status", help="show security configuration and armed state")
        return parser

    # -- entry point -------------------------------------------------------- #
    def run(self, argv: list[str] | None = None) -> int:
        """Parse ``argv`` and execute the selected subcommand.

        Args:
            argv: Argument list, defaulting to ``sys.argv[1:]``.

        Returns:
            A process exit code.
        """
        args = self._parser.parse_args(argv)
        handler = getattr(self, f"_cmd_{args.command}")
        try:
            return handler(args)
        except HueIacError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    # -- subcommands -------------------------------------------------------- #
    def _cmd_validate(self, args: argparse.Namespace) -> int:
        """Validate the configuration file."""
        config = load_config(args.config)
        print(
            f"ok: {len(config.motion_policies)} motion polic"
            f"{'y' if len(config.motion_policies) == 1 else 'ies'} validated"
        )
        return 0

    def _cmd_preview(self, args: argparse.Namespace) -> int:
        """Print the circadian colour curve for a date."""
        config = load_config(args.config)
        day = _dt.date.fromisoformat(args.date) if args.date else _dt.date.today()
        self._print_curve(config, day)
        return 0

    def _cmd_auth(self, args: argparse.Namespace) -> int:
        """Create an application key after the link button is pressed."""
        config = load_config(args.config)
        client = HueClient(config.bridge)
        print("Press the bridge's link button, then press Enter...", end="", flush=True)
        input()
        result = client.create_application_key()
        print("Application key created. Store it securely, for example:")
        print(f"\n    export HUE_APPLICATION_KEY={result['username']}\n")
        return 0

    def _cmd_inventory(self, args: argparse.Namespace) -> int:
        """List the bridge's rooms, zones, lights and motion sensors.

        Use the exact names printed here for the ``sensor``, ``rooms`` and
        ``lights`` fields in your config.
        """
        config = load_config(args.config)
        client = HueClient(config.bridge)
        state = BridgeState(client).load()
        self._print_inventory(state)
        self._print_resource_census(client)
        return 0

    def _cmd_plan(self, args: argparse.Namespace) -> int:
        """Show the plan without applying it."""
        config, planner = self._build_planner(args)
        changes = planner.plan()
        self._print_plan(changes)
        return 0

    def _cmd_apply(self, args: argparse.Namespace) -> int:
        """Apply the plan after optional confirmation."""
        config, planner = self._build_planner(args)
        changes = planner.plan()
        self._print_plan(changes)

        blocked = [change for change in changes if change.is_blocked]
        if blocked and not args.ignore_unassigned:
            print(self._format_blocked(blocked))
            return 1

        actionable = [change for change in changes if change.is_actionable]
        if not actionable:
            print("\nNothing to do; bridge already matches config.")
            return 0
        if not args.yes and not self._confirm(len(actionable)):
            print("Aborted.")
            return 1
        applied = planner.apply(changes)
        print(f"\nApplied {applied} change(s).")
        return 0

    def _cmd_watch(self, args: argparse.Namespace) -> int:
        """Run the live motion/timing controller."""
        from .watch import MotionController  # local import keeps startup light

        config = load_config(args.config)
        client = HueClient(config.bridge)
        state = BridgeState(client).load()
        controller = MotionController(client, state, config, dry_run=args.dry_run)
        mode = " (dry-run)" if args.dry_run else ""
        print(f"hueman watching {len(config.motion_policies)} policy/policies{mode}; Ctrl-C to stop.")
        try:
            controller.run()
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    def _cmd_circadian(self, args: argparse.Namespace) -> int:
        """Run the circadian daemon or signal a running daemon to resume."""
        from pathlib import Path

        config = load_config(args.config)
        if config.circadian_daemon is None:
            raise HueIacError("no 'circadian_daemon' block in config")
        if args.circadian_cmd == "resume":
            Path(config.circadian_daemon.control_file).write_text("resume\n")
            print(f"resume signalled via {config.circadian_daemon.control_file}")
            return 0
        from .circadian_daemon import CircadianDaemon

        client = HueClient(config.bridge)
        state = BridgeState(client).load()
        daemon = CircadianDaemon(client, state, config)
        print(f"hueman circadian daemon driving '{config.circadian_daemon.zone}'; Ctrl-C to stop.")
        try:
            daemon.run()
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    def _cmd_security(self, args: argparse.Namespace) -> int:
        """Arm/disarm security mode via its control-files, or print its status."""
        from pathlib import Path

        config = load_config(args.config)
        if config.security is None:
            raise HueIacError("no 'security' block in config")
        sec = config.security
        if args.security_cmd == "on":
            if not sec.file_on:
                raise HueIacError(
                    "security.triggers.control_file.on_file is not set; cannot arm via CLI")
            Path(sec.file_on).write_text("on\n")
            print(f"security armed via {sec.file_on}")
            return 0
        if args.security_cmd == "off":
            if not sec.file_off:
                raise HueIacError(
                    "security.triggers.control_file.off_file is not set; cannot disarm via CLI")
            Path(sec.file_off).write_text("off\n")
            print(f"security disarm signalled via {sec.file_off}")
            return 0
        # status
        pending = bool(sec.file_on and Path(sec.file_on).exists())
        print(f"security groups: {', '.join(sec.groups)}")
        print(f"  on_file:  {sec.file_on or '(unset)'}")
        print(f"  off_file: {sec.file_off or '(unset)'}")
        print(f"  pending on-file present: {pending}")
        return 0

    # -- helpers ------------------------------------------------------------ #
    def _build_planner(self, args: argparse.Namespace) -> tuple[Config, Planner]:
        """Load config, connect, and build a planner over live bridge state."""
        config = load_config(args.config)
        client = HueClient(config.bridge)
        state = BridgeState(client).load()
        return config, Planner(client, state, config)

    @staticmethod
    def _print_plan(changes: list[Change]) -> None:
        """Render a plan in a Terraform-like summary form."""
        symbols = {"create": "+", "update": "~", "noop": " ", "blocked": "!"}
        print("Plan:")
        for change in changes:
            symbol = symbols.get(change.change_type.value, "?")
            print(f"  {symbol} {change.resource} [{change.name}]: {change.summary}")
        actionable = sum(1 for change in changes if change.is_actionable)
        blocked = sum(1 for change in changes if change.is_blocked)
        unchanged = len(changes) - actionable - blocked
        print(f"\n{actionable} change(s), {unchanged} unchanged, {blocked} blocked.")

    @staticmethod
    def _print_inventory(state: BridgeState) -> None:
        """Render rooms, zones, lights and sensors for config authoring."""
        rooms = [group for group in state.groups if group.rtype == "room"]
        zones = [group for group in state.groups if group.rtype == "zone"]

        print(f"Rooms ({len(rooms)}):")
        for room in sorted(rooms, key=_group_name):
            print(f"  - {room.name}  ({len(room.light_rids)} light(s))")
        print(f"\nZones ({len(zones)}):")
        for zone in sorted(zones, key=_group_name):
            print(f"  - {zone.name}  ({len(zone.light_rids)} light(s))")

        print(f"\nLights ({len(state.lights)}):")
        for light in sorted(state.lights, key=_light_name):
            home = light.room_name or "UNASSIGNED"
            print(f"  - {light.name}  [room: {home}]")

        print(f"\nMotion sensors — legacy/PIR ({len(state.sensors)}):")
        for sensor in sorted(state.sensors, key=_sensor_name):
            lux = "yes" if sensor.light_level_rid else "no"
            print(f"  - {sensor.name}  [light-level sensor: {lux}]")

        areas = state.motion_areas
        print(f"\nMotionAware areas — bulb-grid sensing ({len(areas)}):")
        for area in sorted(areas, key=lambda a: a.name):
            room = area.room_name or "?"
            sens = f"{area.sensitivity}/{area.sensitivity_max}" if area.sensitivity is not None else "?"
            now = "MOTION" if area.motion else "clear"
            print(
                f"  - {area.name}  [room: {room}; {area.participant_count} bulbs; "
                f"sensitivity {sens}; now: {now}]"
            )

    @staticmethod
    def _print_resource_census(client: HueClient) -> None:
        """Print a count of EVERY resource type the bridge exposes.

        The authoritative full census, so a resource kind this tool does not yet
        model (e.g. MotionAware) is never silently invisible in inventory.
        """
        from collections import Counter

        counts = Counter(r.get("type", "?") for r in client.get_all_resources())
        print(f"\nAll bridge resource types ({len(counts)}):")
        for rtype, n in sorted(counts.items()):
            print(f"  {n:3}x {rtype}")

    @staticmethod
    def _print_curve(config: Config, day: _dt.date) -> None:
        """Print the circadian curve sampled across ``day``."""
        from .circadian import CircadianCurve
        from .sun import SolarCalculator

        solar = SolarCalculator(
            config.location.lat, config.location.lon, config.location.tz_offset_hours,
            tz=config.location.tz,
        )
        curve = CircadianCurve(config.circadian)
        print(f"Circadian curve for {day.isoformat()} at "
              f"{config.location.lat:.3f},{config.location.lon:.3f}:")
        for minute, sample in curve.sample_day(solar, day, step_minutes=60):
            hour = f"{minute // 60:02d}:{minute % 60:02d}"
            print(f"  {hour}  {sample.kelvin:>5d} K  {sample.brightness:>5.1f}% brightness")

    @staticmethod
    def _format_blocked(blocked: list[Change]) -> str:
        """Render a refusal that lists each blocked change's real reason.

        Replaces the old blanket "lights not assigned" message, which misreported
        every block (smart-scene, night-motion, empty-floor) as an unassigned light.
        ``--ignore-unassigned`` still bypasses all blocked changes, leaving them
        unapplied — which is what enables the two-pass cold-start apply.
        """
        lines = [f"\nRefusing to apply: {len(blocked)} blocked change(s) need attention:"]
        for change in blocked:
            lines.append(f"  ! {change.resource} [{change.name}]: {change.summary}")
        lines.append("Resolve these, or pass --ignore-unassigned to apply the rest.")
        return "\n".join(lines)

    @staticmethod
    def _confirm(count: int) -> bool:
        """Prompt the user to confirm applying ``count`` changes."""
        answer = input(f"\nApply {count} change(s)? [y/N] ").strip().lower()
        return answer in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    """Module entry point used by the ``hueman`` console script."""
    return Cli().run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
