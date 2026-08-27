"""Tests for configuration parsing and validation."""

from __future__ import annotations

import pytest

from hueman.config import BiasLight, Color, Config, SecuritySpec, parse_duration, parse_time_ref
from hueman.errors import ConfigError
from tests.conftest import make_config


def test_parse_duration_units() -> None:
    """Durations parse across ms/s/m/h and bare seconds."""
    assert parse_duration("500ms", ctx="x") == 500
    assert parse_duration("90s", ctx="x") == 90_000
    assert parse_duration("2m", ctx="x") == 120_000
    assert parse_duration("1h", ctx="x") == 3_600_000
    assert parse_duration(30, ctx="x") == 30_000


def test_parse_duration_rejects_garbage() -> None:
    """An unparseable duration raises a config error."""
    with pytest.raises(ConfigError):
        parse_duration("soon", ctx="x")


def test_parse_time_ref_accepts_sun_and_clock() -> None:
    """Time references accept clock times and sun anchors."""
    assert parse_time_ref("sunrise", ctx="x") == "sunrise"
    assert parse_time_ref("23:30", ctx="x") == "23:30"
    with pytest.raises(ConfigError):
        parse_time_ref("25:00", ctx="x")


def test_color_kelvin_converts_to_mirek() -> None:
    """A kelvin colour converts and clamps into the valid mirek range."""
    color = Color.parse({"kelvin": 2700}, "x")
    assert color.mode == "ct"
    assert 360 <= (color.mirek or 0) <= 375


def test_color_hex_validation() -> None:
    """Hex colours validate to six hex digits."""
    assert Color.parse({"hex": "#ff2200"}, "x").hex == "ff2200"
    with pytest.raises(ConfigError):
        Color.parse({"hex": "red"}, "x")


def test_full_policy_round_trip() -> None:
    """A representative policy parses into typed objects."""
    config = make_config(
        {
            "name": "Office",
            "sensor": "Office sensor",
            "rooms": ["Office"],
            "sensitivity": "high",
            "dim_before_off": {"duration": "20s"},
            "timeslots": [
                {"name": "day", "start": "sunrise", "on_motion": "circadian", "timeout": "10m"},
                {
                    "name": "night",
                    "start": "23:00",
                    "on_motion": {"brightness": 8, "color": {"hex": "#ff2a00"}},
                    "timeout": "90s",
                    "standby": {"on": False},
                },
            ],
        }
    )
    policy = config.motion_policies[0]
    assert policy.sensitivity == 2
    assert policy.dim_before_off is not None
    assert policy.timeslots[0].on_motion.color is not None
    assert policy.timeslots[0].on_motion.color.mode == "circadian"
    assert policy.timeslots[1].standby is not None
    assert policy.timeslots[1].standby.on is False


def _cfg_doc(extra: dict) -> dict:
    """A minimal valid top-level config doc, merged with ``extra``."""
    doc = {
        "bridge": {"host": "192.0.2.10", "application_key": "k"},
        "location": {"lat": 45.5, "lon": -122.7, "tz_offset_hours": -7},
        "motion_policies": [],
    }
    doc.update(extra)
    return doc


def test_circadian_scene_parses_full() -> None:
    """A full circadian_scene block parses into a typed spec."""
    from hueman.config import Config

    cfg = Config.parse(_cfg_doc({"circadian_scene": {
        "smart_scene": "Golden hours", "zone": "Night Guide",
        "transition": "ramp", "hand_off": "22:34"}}))
    cs = cfg.circadian_scene
    assert cs is not None
    assert cs.smart_scene == "Golden hours"
    assert cs.zone == "Night Guide"
    assert cs.transition_ms is None  # "ramp" -> use the circadian ramp width
    assert cs.hand_off_min == 22 * 60 + 34


def test_circadian_scene_explicit_transition_and_default_handoff() -> None:
    """An explicit duration parses to ms; hand_off defaults to 22:34."""
    from hueman.config import Config

    cfg = Config.parse(_cfg_doc({"circadian_scene": {
        "smart_scene": "X", "zone": "Z", "transition": "90m"}}))
    cs = cfg.circadian_scene
    assert cs.transition_ms == 90 * 60 * 1000
    assert cs.hand_off_min == 22 * 60 + 34


def test_circadian_scene_requires_zone() -> None:
    """A circadian_scene without a zone is rejected."""
    from hueman.config import Config

    with pytest.raises(ConfigError):
        Config.parse(_cfg_doc({"circadian_scene": {"smart_scene": "X"}}))


def test_circadian_scene_rejects_sun_handoff() -> None:
    """hand_off must be a clock time, not a sun anchor."""
    from hueman.config import Config

    with pytest.raises(ConfigError):
        Config.parse(_cfg_doc({"circadian_scene": {
            "smart_scene": "X", "zone": "Z", "hand_off": "sunset"}}))


def test_no_circadian_scene_is_none() -> None:
    """Omitting circadian_scene leaves it unset."""
    from hueman.config import Config

    assert Config.parse(_cfg_doc({})).circadian_scene is None


def test_duplicate_policy_names_rejected() -> None:
    """Two policies with the same name fail validation."""
    base = {
        "name": "Dup",
        "sensor": "S",
        "rooms": ["R"],
        "timeslots": [{"name": "d", "start": "07:00", "on_motion": "circadian", "timeout": "1m"}],
    }
    doc = {
        "bridge": {"host": "192.0.2.10", "application_key": "k"},
        "location": {"lat": 1, "lon": 2, "tz_offset_hours": 0},
        "motion_policies": [base, dict(base)],
    }
    with pytest.raises(ConfigError):
        from hueman.config import Config

        Config.parse(doc)


def test_location_tz_only_backfills_a_float_offset() -> None:
    """`tz` without tz_offset_hours parses; offset is back-filled to a float."""
    from hueman.config import Config

    cfg = Config.parse(_cfg_doc(
        {"location": {"lat": 45.5, "lon": -122.7, "tz": "America/Los_Angeles"}}
    ))
    assert cfg.location.tz == "America/Los_Angeles"
    assert cfg.location.tz_offset_hours in (-8.0, -7.0)  # PST or PDT depending on run date


def test_location_tz_and_offset_both_retained() -> None:
    """When both are given, both are kept (tz drives per-date derivation downstream)."""
    from hueman.config import Config

    cfg = Config.parse(_cfg_doc(
        {"location": {"lat": 45.5, "lon": -122.7, "tz_offset_hours": -7, "tz": "America/Los_Angeles"}}
    ))
    assert cfg.location.tz == "America/Los_Angeles"
    assert cfg.location.tz_offset_hours == -7.0


def test_location_unknown_tz_rejected() -> None:
    """A bogus IANA name is a config error, not a runtime crash."""
    from hueman.config import Config

    with pytest.raises(ConfigError):
        Config.parse(_cfg_doc({"location": {"lat": 45.5, "lon": -122.7, "tz": "Not/A_Zone"}}))


def test_location_requires_tz_or_offset() -> None:
    """Neither tz nor tz_offset_hours -> ConfigError (unchanged contract)."""
    from hueman.config import Config

    with pytest.raises(ConfigError):
        Config.parse(_cfg_doc({"location": {"lat": 45.5, "lon": -122.7}}))


def test_location_empty_or_malformed_tz_rejected() -> None:
    """Empty string and path-like tz values are ConfigErrors, not ValueError crashes."""
    from hueman.config import Config

    with pytest.raises(ConfigError):
        Config.parse(_cfg_doc({"location": {"lat": 45.5, "lon": -122.7, "tz": ""}}))

    with pytest.raises(ConfigError):
        Config.parse(_cfg_doc({"location": {"lat": 45.5, "lon": -122.7, "tz": "../etc"}}))


def test_location_tz_must_be_a_string() -> None:
    """A non-string tz (e.g. an integer) is a ConfigError."""
    from hueman.config import Config

    with pytest.raises(ConfigError):
        Config.parse(_cfg_doc({"location": {"lat": 45.5, "lon": -122.7, "tz": 42}}))


def test_circadian_daemon_defaults():
    from hueman.config import Config
    cfg = Config.parse(_cfg_doc({"circadian_daemon": {"zone": "Night Guide"}}))
    d = cfg.circadian_daemon
    assert d.zone == "Night Guide"
    assert d.interval_ms == 60_000          # default 60s
    assert d.transition_ms == 75_000        # default 75s
    assert d.fade_off_ms == 90_000          # default 90s
    assert d.hand_off_min == 22 * 60 + 34   # default 22:34
    assert d.start.base == "sunrise" and d.start.value == 0
    assert d.detect_override is True
    assert d.echo_ttl_ms == 4_000
    assert d.resume_on_power_cycle is True
    assert d.resume_trigger is None
    assert d.control_file == ".hue-circadian-resume"
    # off by default: a manual override holds until an explicit action
    # (power-cycle / resume trigger / restart), never a silent morning retake
    assert d.daily_safety_resume is False
    assert d.brightness_floor is None and d.brightness_ceiling is None
    assert d.retry_on_error_ms == 30_000
    assert d.sse_backoff_max_ms == 60_000
    assert d.log_path == "logs/circadian.log" and d.log_level == "info"
    # settle-and-compare override detection defaults
    assert d.override_band == 8.0
    assert d.settle_window_ms == 2_500
    assert d.settle_epsilon == 0.75
    assert d.night_look is None             # default: hand-off fades the zone off


def test_circadian_daemon_night_look():
    from hueman.config import Config
    cfg = Config.parse(_cfg_doc({"circadian_daemon": {
        "zone": "Z", "night_look": {"brightness": 1, "hex": "#ff0000"}}}))
    look = cfg.circadian_daemon.night_look
    assert look is not None and look.on is True
    assert look.brightness == 1.0
    assert look.color is not None and look.color.hex is not None


def test_circadian_daemon_night_look_requires_brightness_and_static_colour():
    from hueman.config import Config
    with pytest.raises(ConfigError):   # no brightness
        Config.parse(_cfg_doc({"circadian_daemon": {
            "zone": "Z", "night_look": {"hex": "#ff0000"}}}))
    with pytest.raises(ConfigError):   # no colour
        Config.parse(_cfg_doc({"circadian_daemon": {
            "zone": "Z", "night_look": {"brightness": 1}}}))
    with pytest.raises(ConfigError):   # circadian isn't a static look
        Config.parse(_cfg_doc({"circadian_daemon": {
            "zone": "Z", "night_look": {"brightness": 1, "color": "circadian"}}}))


def test_circadian_daemon_overrides_and_anchor():
    from hueman.config import Config
    cfg = Config.parse(_cfg_doc({"circadian_daemon": {
        "zone": "Z", "start": "sunrise+30m", "hand_off": "23:00",
        "interval": "90s", "transition": "120s", "fade_off": "2m",
        "manual_override": {"detect": False, "echo_ttl": "6s", "resume_on_power_cycle": False,
                            "resume_trigger": "Circadian", "control_file": "/tmp/r",
                            "daily_safety_resume": False,
                            "override_band": 12, "settle_window": "3s", "settle_epsilon": 1.5},
        "brightness_floor": 5, "brightness_ceiling": 95,
        "retry": {"on_error": "10s", "sse_backoff_max": "45s"},
        "log": {"path": "/var/log/c.log", "level": "debug"}}}))
    d = cfg.circadian_daemon
    assert d.start.base == "sunrise" and d.start.value == 30
    assert d.hand_off_min == 23 * 60
    assert d.interval_ms == 90_000 and d.transition_ms == 120_000 and d.fade_off_ms == 120_000
    assert d.detect_override is False and d.echo_ttl_ms == 6_000
    assert d.resume_on_power_cycle is False and d.resume_trigger == "Circadian"
    assert d.control_file == "/tmp/r" and d.daily_safety_resume is False
    assert d.brightness_floor == 5.0 and d.brightness_ceiling == 95.0
    assert d.retry_on_error_ms == 10_000 and d.sse_backoff_max_ms == 45_000
    assert d.log_path == "/var/log/c.log" and d.log_level == "debug"
    # settle-and-compare override detection overrides
    assert d.override_band == 12.0
    assert d.settle_window_ms == 3_000
    assert d.settle_epsilon == 1.5


def test_circadian_daemon_requires_zone():
    from hueman.config import Config
    with pytest.raises(ConfigError):
        Config.parse(_cfg_doc({"circadian_daemon": {}}))


def test_circadian_daemon_absent_is_none():
    from hueman.config import Config
    assert Config.parse(_cfg_doc({})).circadian_daemon is None


# --------------------------------------------------------------------------- #
# circadian_daemon.bias (daemon-native TV bias hold)
# --------------------------------------------------------------------------- #
def _daemon_bias(bias: dict):
    """Parse a config whose circadian_daemon carries the given bias block."""
    from hueman.config import Config
    cfg = Config.parse(_cfg_doc({"circadian_daemon": {"zone": "Night Guide", "bias": bias}}))
    return cfg.circadian_daemon.bias


def test_bias_parses_full() -> None:
    from hueman.config import BiasSpec
    bias = _daemon_bias({
        "lights": {
            "Play bars": {"look": {"mirek": 153, "brightness": 28}, "idle": "off"},
            "Couch Lightstrip": {"look": {"hex": "1a0a00", "brightness": 5}, "idle": "circadian"},
        },
        "triggers": {
            "sse": {"on_trigger": "TV On", "off_trigger": "TV Off"},
            "control_file": {"on_file": ".tv-on", "off_file": ".tv-off"},
            "probe": {"enabled": True, "host": "192.0.2.50", "mode": "tcp",
                      "port": 3001, "interval": "5s", "debounce": "5s"},
        },
    })
    assert isinstance(bias, BiasSpec)
    by_name = {light.name: light for light in bias.lights}
    assert set(by_name) == {"Play bars", "Couch Lightstrip"}
    assert by_name["Play bars"].idle == "off"
    assert by_name["Play bars"].look.color.mirek == 153
    assert by_name["Play bars"].look.brightness == 28.0
    assert by_name["Couch Lightstrip"].idle == "circadian"
    assert by_name["Couch Lightstrip"].look.color.hex == "1a0a00"
    assert bias.sse_on == "TV On" and bias.sse_off == "TV Off"
    assert bias.file_on == ".tv-on" and bias.file_off == ".tv-off"
    assert bias.probe_enabled is True and bias.probe_host == "192.0.2.50"
    assert bias.probe_mode == "tcp" and bias.probe_port == 3001
    assert bias.probe_interval_ms == 5_000 and bias.probe_debounce_ms == 5_000


def test_bias_defaults() -> None:
    bias = _daemon_bias({"lights": {"Couch": {"look": {"mirek": 300, "brightness": 10}}}})
    assert bias.probe_enabled is False
    assert bias.lights[0].idle == "circadian"          # default idle
    assert bias.sse_on is None and bias.file_on is None
    assert bias.probe_mode == "tcp" and bias.probe_port == 3001


def test_bias_look_requires_colour() -> None:
    with pytest.raises(ConfigError):
        _daemon_bias({"lights": {"X": {"look": {"brightness": 50}}}})


def test_bias_look_rejects_circadian() -> None:
    with pytest.raises(ConfigError):
        _daemon_bias({"lights": {"X": {"look": "circadian"}}})


def test_bias_rejects_bad_idle() -> None:
    with pytest.raises(ConfigError):
        _daemon_bias({"lights": {"X": {"look": {"mirek": 300, "brightness": 10}, "idle": "bogus"}}})


def test_bias_accepts_unquoted_off_idle() -> None:
    """YAML 1.1 coerces an unquoted ``idle: off`` to boolean False; accept it as "off"."""
    bias = _daemon_bias({"lights": {"X": {"look": {"mirek": 300, "brightness": 10}, "idle": False}}})
    assert bias.lights[0].idle == "off"


def test_bias_requires_lights() -> None:
    with pytest.raises(ConfigError):
        _daemon_bias({"lights": {}})


def test_bias_edge_transition_defaults_to_2s() -> None:
    bias = _daemon_bias({"lights": {"Couch": {"look": {"mirek": 300, "brightness": 10}}}})
    assert bias.transition_ms == 2_000


def test_bias_edge_transition_explicit() -> None:
    bias = _daemon_bias({
        "lights": {"Couch": {"look": {"mirek": 300, "brightness": 10}}},
        "transition": "3s",
    })
    assert bias.transition_ms == 3_000


def test_bias_absent_is_none() -> None:
    from hueman.config import Config
    cfg = Config.parse(_cfg_doc({"circadian_daemon": {"zone": "Night Guide"}}))
    assert cfg.circadian_daemon.bias is None


# --------------------------------------------------------------------------- #
# security: block
# --------------------------------------------------------------------------- #
def _sec_doc(security):
    return {
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5, "lon": -122.6, "tz_offset_hours": -7},
        "motion_policies": [],
        "security": security,
    }


def test_security_spec_parses_full_block():
    cfg = Config.parse(_sec_doc({
        "groups": ["Night Guide", "TV Viewing"],
        "alert": {"seconds": 8, "color": "#ff0000", "min_brightness": 30, "breathe_hz": 0.5},
        "chaos": {"frame_interval": "250ms", "min_flash_interval": "350ms"},
        "max_duration": "10m",
        "triggers": {
            "control_file": {"on_file": "logs/.sec-on", "off_file": "logs/.sec-off"},
            "sse": {"on_trigger": "Panic On", "off_trigger": "Panic Off"},
            "poll_interval": "1s",
        },
        "sound": {"cue_file": "logs/.sec-cue", "webhook": "http://ha/x"},
    }))
    s = cfg.security
    assert s.groups == ("Night Guide", "TV Viewing")
    assert s.alert_seconds == 8 and s.alert_color == "ff0000"
    assert s.alert_min_brightness == 30.0 and s.alert_breathe_hz == 0.5
    assert s.frame_interval_ms == 250 and s.min_flash_interval_ms == 350
    assert s.max_duration_ms == 600_000
    assert (s.file_on, s.file_off) == ("logs/.sec-on", "logs/.sec-off")
    assert (s.sse_on, s.sse_off) == ("Panic On", "Panic Off")
    assert s.cue_file == "logs/.sec-cue" and s.cue_webhook == "http://ha/x"
    assert s.poll_interval_ms == 1000


def test_security_defaults_apply():
    cfg = Config.parse(_sec_doc({"groups": ["All"]}))
    s = cfg.security
    assert s.alert_seconds == 10 and s.alert_color == "ff0000"
    assert s.alert_min_brightness == 40.0 and s.alert_breathe_hz == 0.5
    assert s.frame_interval_ms == 250 and s.min_flash_interval_ms == 350
    assert s.max_duration_ms == 600_000 and s.poll_interval_ms == 1000
    assert s.file_on is None and s.cue_file is None


def test_security_absent_is_none():
    doc = _sec_doc({"groups": ["x"]})
    del doc["security"]
    assert Config.parse(doc).security is None


def test_security_requires_a_group():
    with pytest.raises(ConfigError, match="at least one group"):
        Config.parse(_sec_doc({"groups": []}))


def test_security_flash_floor_rejected():
    with pytest.raises(ConfigError, match="min_flash_interval"):
        Config.parse(_sec_doc({"groups": ["All"], "chaos": {"min_flash_interval": "200ms"}}))


def test_security_breathe_hz_capped():
    with pytest.raises(ConfigError, match="breathe_hz"):
        Config.parse(_sec_doc({"groups": ["All"], "alert": {"breathe_hz": 5}}))


def test_security_max_duration_must_exceed_alert():
    with pytest.raises(ConfigError, match="max_duration"):
        Config.parse(_sec_doc({"groups": ["All"], "alert": {"seconds": 30}, "max_duration": "10s"}))


def test_security_bad_color_rejected():
    with pytest.raises(ConfigError, match="color"):
        Config.parse(_sec_doc({"groups": ["All"], "alert": {"color": "red"}}))
# -- validation-leak regressions (2026-07-02 review) -------------------------- #
def test_kelvin_zero_is_config_error() -> None:
    """`kelvin: 0` must be a ConfigError with the ctx path, not ZeroDivisionError."""
    with pytest.raises(ConfigError, match="kelvin"):
        Color.parse({"kelvin": 0}, "policy.look")


def test_kelvin_non_numeric_is_config_error() -> None:
    with pytest.raises(ConfigError, match="kelvin"):
        Color.parse({"kelvin": "warm"}, "policy.look")


def test_mirek_non_numeric_is_config_error() -> None:
    with pytest.raises(ConfigError, match="mirek"):
        Color.parse({"mirek": "warm"}, "policy.look")


def test_bias_brightness_non_numeric_is_config_error() -> None:
    with pytest.raises(ConfigError, match="brightness"):
        _daemon_bias({"lights": {"X": {"look": {"mirek": 300, "brightness": "dim"}}}})


def test_duration_rejects_negative() -> None:
    with pytest.raises(ConfigError):
        parse_duration(-5, ctx="daemon.interval")
    with pytest.raises(ConfigError):
        parse_duration(-0.5, ctx="daemon.interval")


def test_duration_rejects_yaml_bool() -> None:
    """YAML `interval: yes` parses as True; bool is an int subclass -- reject it."""
    with pytest.raises(ConfigError):
        parse_duration(True, ctx="daemon.interval")


def test_circadian_night_start_is_rejected_as_removed() -> None:
    """`circadian.night_start` was a parsed-but-dead knob; setting it must fail
    loudly instead of silently doing nothing."""
    doc = _cfg_doc({})
    doc["circadian"] = {"day_mirek": 233, "night_start": "23:00"}
    from hueman.config import Config
    with pytest.raises(ConfigError, match="night_start"):
        Config.parse(doc)

def test_security_lights_per_frame_default_and_floor():
    cfg = Config.parse(_sec_doc({"groups": ["All"]}))
    assert cfg.security.lights_per_frame == 3
    cfg = Config.parse(_sec_doc({"groups": ["All"], "chaos": {"lights_per_frame": 5}}))
    assert cfg.security.lights_per_frame == 5
    with pytest.raises(ConfigError, match="lights_per_frame"):
        Config.parse(_sec_doc({"groups": ["All"], "chaos": {"lights_per_frame": 0}}))


def test_bias_light_parses_optional_night_look() -> None:
    """night_look parses with the same rules as look and stays optional."""
    light = BiasLight.parse(
        "Couch strip",
        {"look": {"mirek": 400, "brightness": 24},
         "night_look": {"mirek": 454, "brightness": 8},
         "idle": "circadian"},
        "ctx",
    )
    assert light.night_look is not None
    assert light.night_look.brightness == 8.0
    assert light.night_look.color.mirek == 454
    bare = BiasLight.parse("Bars", {"look": {"mirek": 153, "brightness": 95}}, "ctx")
    assert bare.night_look is None


def test_bias_light_night_look_is_validated_like_look() -> None:
    """A night_look is a hold: explicit in-range brightness and a colour required."""
    with pytest.raises(ConfigError, match=r"night_look.*brightness"):
        BiasLight.parse(
            "Couch strip",
            {"look": {"mirek": 400, "brightness": 24},
             "night_look": {"mirek": 454}},
            "ctx",
        )
    with pytest.raises(ConfigError, match=r"night_look.*brightness must be 0-100"):
        BiasLight.parse(
            "Couch strip",
            {"look": {"mirek": 400, "brightness": 24},
             "night_look": {"mirek": 454, "brightness": 180}},
            "ctx",
        )
