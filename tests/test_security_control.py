"""Tests for the pure security-mode decision core."""

from __future__ import annotations

from hue_iac.config import Config
from hue_iac.security_control import (
    PHASE_ALERT,
    PHASE_CHAOS,
    SecurityController,
    unknown_security_groups,
)


def _spec(**over):
    sec = {
        "groups": over.get("groups", ["A", "B"]),
        "alert": {
            "seconds": over.get("alert_seconds", 10),
            "color": over.get("color", "#ff0000"),
            "min_brightness": over.get("min_brightness", 40),
            "breathe_hz": over.get("breathe_hz", 0.5),
        },
        "chaos": {
            "frame_interval": over.get("frame_interval", "250ms"),
            "min_flash_interval": over.get("min_flash_interval", "350ms"),
        },
        "max_duration": over.get("max_duration", "10m"),
    }
    doc = {
        "bridge": {"host": "x", "application_key": "k"},
        "location": {"lat": 45.5, "lon": -122.6, "tz_offset_hours": -7},
        "motion_policies": [],
        "security": sec,
    }
    return Config.parse(doc).security


def test_phase_boundary_at_alert_seconds():
    c = SecurityController(_spec(alert_seconds=10))
    assert c.phase_at(0) == PHASE_ALERT
    assert c.phase_at(9_999) == PHASE_ALERT
    assert c.phase_at(10_000) == PHASE_CHAOS
    assert c.phase_at(60_000) == PHASE_CHAOS


def test_is_expired_at_max_duration():
    c = SecurityController(_spec(max_duration="2m"))
    assert c.is_expired(119_000) is False
    assert c.is_expired(120_000) is True


def test_alert_frame_is_red_breathe_across_all_groups():
    c = SecurityController(_spec(alert_seconds=10, color="#ff0000",
                                min_brightness=40, breathe_hz=0.5))
    f0 = c.frame_at(0, 0)
    assert f0.phase == PHASE_ALERT
    assert {g.name for g in f0.targets} == {"A", "B"}
    for g in f0.targets:
        assert g.target.hex == "ff0000" and g.target.on is True
    # breathe: trough at t=0 -> min_brightness; peak at half-period (1s for 0.5Hz) -> 100
    assert f0.targets[0].target.brightness == 40.0
    peak = c.frame_at(1_000, 4)   # 0.5 Hz -> half-cycle at 1.0s
    assert peak.targets[0].target.brightness == 100.0


def test_chaos_frame_is_deterministic():
    c = SecurityController(_spec())
    a = c.frame_at(20_000, 80)
    b = c.frame_at(20_000, 80)
    assert a == b
    assert a.phase == PHASE_CHAOS


def test_chaos_groups_are_out_of_phase():
    # With two groups the alternation is by even/odd group index: group A (index 0)
    # and group B (index 1) are always on opposite brightness levels within any block.
    # Sample across a block boundary to confirm the invariant holds in both block states.
    c = SecurityController(_spec(groups=["A", "B"],
                                 frame_interval="250ms", min_flash_interval="350ms"))
    # flash_period_frames = 2; block 24 (frames 48-49) and block 25 (frames 50-51)
    for frame_index in (48, 50):          # one frame per block, on either side of the boundary
        f = c.frame_at(12_000, frame_index)
        bris = sorted(g.target.brightness for g in f.targets)
        assert bris[0] != bris[1], (
            f"frame {frame_index}: expected A and B to be out of phase, got {bris}")


def test_chaos_brightness_respects_flash_cap():
    # SAFETY INVARIANT: a group's brightness is constant within each block of
    # flash_period_frames = ceil(350/250) = 2 frames, so it toggles no faster
    # than every 2 frames (500ms => well below 3 Hz).
    c = SecurityController(_spec(frame_interval="250ms", min_flash_interval="350ms"))
    period = 2
    # Collect group "A" brightness across 60 chaos frames (start well past alert).
    base = 80
    bri = {}
    for k in range(60):
        f = c.frame_at(20_000 + k * 250, base + k)
        bri[base + k] = next(g.target.brightness for g in f.targets if g.name == "A")
    # within every period-block the brightness must be identical
    for block_start in range(base, base + 60 - period, period):
        block = [bri[i] for i in range(block_start, block_start + period)]
        assert len(set(block)) == 1, f"brightness flashed within a {period}-frame block: {block}"


def test_chaos_brightness_flips_at_block_boundaries():
    # SAFETY INVARIANT is luminance-only (hue thrashes every frame by design —
    # see test_chaos_hue_thrashes_while_brightness_holds_within_block): brightness
    # is constant within each flash block and flips when the block advances.
    # flash_period_frames = ceil(350/250) = 2: frames 80/81 share block 40, 82 starts 41.
    c = SecurityController(_spec(frame_interval="250ms", min_flash_interval="350ms"))
    bri = lambda f: {t.name: t.target.brightness for t in c.frame_at(20_000, f).targets}
    assert bri(80) == bri(81)
    assert bri(81) != bri(82)


def test_unknown_security_groups_lists_missing_in_order():
    spec = _spec(groups=["A", "Ghost", "B", "Nope"])
    assert unknown_security_groups(spec, ["A", "B", "C"]) == ["Ghost", "Nope"]


# -- chaos rework: snaps, hue-every-update, per-light budget (2026-07-02) ------ #
def _hue_of(hexv: str) -> float:
    import colorsys
    r, g, b = (int(hexv[i:i+2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)[0]


def _lights(n=12):
    return tuple(f"L{i}" for i in range(n))


def test_chaos_hue_thrashes_while_brightness_holds_within_block():
    """Spec restoration: hue changes on EVERY update of a unit; only brightness
    is pinned to the flash-block schedule (the safety cap is luminance-only)."""
    spec = _spec(frame_interval="250ms", min_flash_interval="350ms")
    c = SecurityController(spec)          # group mode: every unit in every frame
    # flash period = ceil(350/250) = 2 frames -> frames 0 and 1 share a block
    f0 = {t.name: t for t in c.frame_at(60_000, 0).targets}
    f1 = {t.name: t for t in c.frame_at(60_250, 1).targets}
    for name in f0:
        assert f0[name].target.brightness == f1[name].target.brightness  # capped
        assert f0[name].target.hex != f1[name].target.hex                # thrashing


def test_chaos_consecutive_hues_are_far_apart():
    """Adjacent updates of the same unit jump >= ~86 degrees of hue -- violent
    complementary-ish cuts, not neighbouring shades."""
    spec = _spec()
    c = SecurityController(spec)
    prev = None
    for i in range(40):
        t = {t.name: t for t in c.frame_at(60_000 + i * 250, i).targets}["A"]
        if t.target.hex == "ffffff":
            prev = None                    # white blasts are deliberate slams, not shades
            continue
        hue = _hue_of(t.target.hex)
        if prev is not None:
            dist = min(abs(hue - prev), 1 - abs(hue - prev))
            assert dist >= 0.2, f"frame {i}: hue jump only {dist:.3f}"
        prev = hue


def test_chaos_per_light_budget_and_rotation():
    """With member lights injected, chaos targets individual lights: at most
    lights_per_frame per frame, kind 'light', and every light is covered
    within one rotation cycle."""
    spec = _spec()
    c = SecurityController(spec, lights=_lights(12))
    seen: set[str] = set()
    for i in range(4):                    # 12 lights / 3 per frame = 4 frames
        frame = c.frame_at(60_000 + i * 250, i)
        assert frame.phase == PHASE_CHAOS
        assert len(frame.targets) <= 3    # default lights_per_frame
        assert all(t.kind == "light" for t in frame.targets)
        seen |= {t.name for t in frame.targets}
    assert seen == set(_lights(12))


def test_chaos_per_light_brightness_respects_cap():
    """A single light's brightness never changes faster than min_flash_interval,
    even across the rotation schedule."""
    spec = _spec(frame_interval="250ms", min_flash_interval="350ms")
    c = SecurityController(spec, lights=_lights(12))
    last_change_ms: dict[str, float] = {}
    last_bri: dict[str, float] = {}
    for i in range(200):
        ms = 60_000 + i * 250
        for t in c.frame_at(ms, i).targets:
            if t.name in last_bri and t.target.brightness != last_bri[t.name]:
                assert ms - last_change_ms[t.name] >= 350, (
                    f"{t.name} luminance flip after {ms - last_change_ms[t.name]}ms")
                last_change_ms[t.name] = ms
            elif t.name not in last_bri:
                last_change_ms[t.name] = ms
            last_bri[t.name] = t.target.brightness


def test_alert_stays_whole_group_even_with_lights_injected():
    """The legible alert phase drives the groups in sync; per-light chaos must
    not leak into it."""
    spec = _spec()
    c = SecurityController(spec, lights=_lights(12))
    f = c.frame_at(0, 0)
    assert f.phase == PHASE_ALERT
    assert {t.name for t in f.targets} == {"A", "B"}
    assert all(t.kind == "group" for t in f.targets)


# -- jarring intensifiers (2026-07-02, round 2) -------------------------------- #
def test_chaos_dim_trough_is_near_black():
    """The dim half of the luminance alternation is a hard trough (~3%), not a
    polite 20% -- bigger contrast per (still cap-limited) flip."""
    spec = _spec()
    c = SecurityController(spec)
    bris = set()
    for i in range(20):
        for t in c.frame_at(60_000 + i * 250, i).targets:
            bris.add(t.target.brightness)
    assert min(bris) <= 5.0, f"dim trough too gentle: {sorted(bris)}"
    assert max(bris) == 100.0


def test_chaos_white_blasts_scatter_through_the_hue_stream():
    """Every 4th update of a unit is a full-white slam; offsets scatter the
    blasts so the room never blasts in unison."""
    spec = _spec()
    c = SecurityController(spec)
    whites_a = [i for i in range(16)
                if {t.name: t for t in c.frame_at(60_000 + i * 250, i).targets}["A"].target.hex == "ffffff"]
    whites_b = [i for i in range(16)
                if {t.name: t for t in c.frame_at(60_000 + i * 250, i).targets}["B"].target.hex == "ffffff"]
    assert whites_a and whites_b            # both units blast
    assert whites_a != whites_b             # ...but not in unison
    assert all(b - a == 3 for a, b in zip(whites_a, whites_a[1:]))  # every 3rd update


def test_first_chaos_frame_slams_every_light_at_once():
    """The alert->chaos boundary is punctuated: the first chaos frame targets
    ALL units simultaneously (one bang), then the rotating budget resumes."""
    spec = _spec(alert_seconds=1)           # alert = 4 frames at 250ms
    c = SecurityController(spec, lights=_lights(12))
    first_chaos = c.frame_at(1_000, 4)      # frame 4 = first past the boundary
    assert first_chaos.phase == PHASE_CHAOS
    assert len(first_chaos.targets) == 12   # the slam
    nxt = c.frame_at(1_250, 5)
    assert len(nxt.targets) <= 3            # budget resumes immediately after


# -- edge strobe: counter-phase zone strobe at the legal-rate limit ------------ #
def test_chaos_alternates_patchwork_and_strobe_patterns():
    """Chaos switches sub-pattern every few seconds: per-light patchwork, then a
    whole-zone counter-phase strobe (kind 'group'), and back — the rhythm change
    is itself disorienting."""
    spec = _spec(alert_seconds=1)
    c = SecurityController(spec, lights=_lights(12))
    kinds = set()
    # sample well past alert across > one full pattern cycle (2 x 4s)
    for i in range(40):
        ms = 2_000 + i * 250
        f = c.frame_at(ms, i)
        kinds.add(frozenset(t.kind for t in f.targets))
    assert frozenset({"light"}) in kinds, "patchwork pattern missing"
    assert frozenset({"group"}) in kinds, "strobe pattern missing"


def test_strobe_flips_at_min_flash_interval_counter_phase():
    """During the strobe pattern both zones flip bright<->near-black in
    counter-phase, each at exactly the min_flash_interval cadence (~2.86 Hz at
    350ms) — the legal edge, never past it."""
    spec = _spec(alert_seconds=1, frame_interval="250ms", min_flash_interval="350ms")
    c = SecurityController(spec, lights=_lights(12))
    # find a strobe window and track each group's luminance flips in wall time
    last_bri: dict[str, float] = {}
    last_flip: dict[str, float] = {}
    saw_counter_phase = False
    total_flips = 0
    for i in range(200):
        ms = 2_000 + i * 250
        f = c.frame_at(ms, i)
        if any(t.kind != "group" for t in f.targets):
            last_bri.clear(); last_flip.clear()   # pattern switch resets tracking
            continue
        bris = {t.name: t.target.brightness for t in f.targets}
        if len(set(bris.values())) == 2:
            saw_counter_phase = True               # zones opposed
        for name, bri in bris.items():
            if name in last_bri and bri != last_bri[name]:
                if name in last_flip:
                    assert ms - last_flip[name] >= 350, (
                        f"{name} strobed after {ms - last_flip[name]}ms (cap!)")
                last_flip[name] = ms
                total_flips += 1
            last_bri[name] = bri
    assert saw_counter_phase
    assert total_flips > 4, "no sustained strobe flipping observed"
