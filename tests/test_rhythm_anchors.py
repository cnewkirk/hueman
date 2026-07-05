"""Tests for the learned-anchor store."""
from hueman.rhythm_control import AnchorStore


def test_empty_store_has_no_median():
    assert AnchorStore().median("wake", "weekday") is None


def test_record_and_median():
    st = AnchorStore()
    for i, m in enumerate([420, 430, 440]):
        st.record("wake", "weekday", m, f"2026-07-0{i + 1}")
    assert st.median("wake", "weekday") == 430


def test_one_sample_per_date_latest_wins():
    st = AnchorStore()
    st.record("wake", "weekday", 400, "2026-07-01")
    st.record("wake", "weekday", 460, "2026-07-01")  # re-observation same day
    assert st.median("wake", "weekday") == 460


def test_capped_at_14_newest_kept():
    st = AnchorStore()
    for day in range(1, 21):  # 20 samples
        st.record("wake", "weekday", 400 + day, f"2026-06-{day:02d}")
    doc = st.to_json()
    samples = doc["anchors"]["weekday"]["wake"]
    assert len(samples) == 14
    assert samples[0]["date"] == "2026-06-07"  # oldest surviving


def test_json_roundtrip():
    st = AnchorStore()
    st.record("sleep_onset", "weekend", 1400, "2026-07-04")
    st2 = AnchorStore.from_json(st.to_json())
    assert st2.median("sleep_onset", "weekend") == 1400


def test_from_json_tolerates_garbage():
    assert AnchorStore.from_json({}).median("wake", "weekday") is None
    assert AnchorStore.from_json({"anchors": {"weekday": {"wake": "nope"}}}
                                 ).median("wake", "weekday") is None
