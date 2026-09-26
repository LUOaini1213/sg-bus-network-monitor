import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from corridors import buses_per_hour  # noqa: E402
from demand import day_counts, surveillance  # noqa: E402
from priority import bearing, load_snapshots, norm_road  # noqa: E402


@pytest.mark.parametrize("band, expected", [("09-12", 60 / 10.5), ("10", 6.0), ("-", 0.0), ("", 0.0), (None, 0.0),
                                            ("00-00", 0.0), ("05-08", 60 / 6.5)])
def test_buses_per_hour(band, expected):
    assert buses_per_hour(band) == pytest.approx(expected)


@pytest.mark.parametrize("ym, wd, we", [("2026-02", 18, 10),   # CNY 17-18 Feb fall on weekdays
                                        ("2026-06", 21, 9),    # Mon 1 Jun (Vesak in lieu)
                                        ("2026-07", 23, 8),
                                        ("2026-08", 20, 11)])  # Mon 10 Aug (National Day in lieu)
def test_day_counts(ym, wd, we):
    assert day_counts(ym) == {"WEEKDAY": wd, "WEEKENDS/HOLIDAY": we}


def test_bearing_quadrants():
    assert bearing(0, 0, 0, 1) == pytest.approx(0)
    assert bearing(0, 0, 1, 0) == pytest.approx(90)
    assert bearing(0, 0, 0, -1) == pytest.approx(180)
    assert bearing(0, 0, -1, 0) == pytest.approx(270)


@pytest.mark.parametrize("a, b", [("Upp S'goon Rd", "UPPER SERANGOON ROAD"), ("AYE", "AYER RAJAH EXPRESSWAY"),
                                  ("Bt Timah Rd", "BUKIT TIMAH ROAD"), ("C'wealth Ave West", "COMMONWEALTH AVENUE WEST"),
                                  ("S'goon Gdn Way", "SERANGOON GARDEN WAY")])
def test_norm_road_matches_speedband_names(a, b):
    assert norm_road(a) == norm_road(b)


def test_norm_road_keeps_distinct_roads_apart():
    assert norm_road("Jln Ahmad Ibrahim") != norm_road("AYER RAJAH EXPRESSWAY")


def _volume(base, latest):
    rows = []
    for stop, (b, l) in enumerate(zip(base, latest)):
        rows += [{"stop": f"{stop:05d}", "YEAR_MONTH": "2026-07", "DAY_TYPE": "WEEKDAY", "tap_in_per_day": b},
                 {"stop": f"{stop:05d}", "YEAR_MONTH": "2026-08", "DAY_TYPE": "WEEKDAY", "tap_in_per_day": l}]
    return pd.DataFrame(rows)


def test_surveillance_flags_only_the_outliers():
    rng = np.random.default_rng(0)
    base = rng.uniform(500, 5000, 200)
    latest = base * rng.normal(1.02, 0.03, 200)   # network-wide +2 % with noise
    latest[5] = base[5] * 3                         # a surge
    latest[9] = base[9] * 0.2                       # a drop
    s = surveillance(_volume(base, latest), "2026-08", "2026-07")
    flagged = dict(zip(s.stop, s.flag))
    assert flagged["00005"] == "surge" and flagged["00009"] == "drop"
    assert sum(f != "" for f in flagged.values()) == 2
    assert s.network_shift.iloc[0] == pytest.approx(0.02, abs=0.01)


def test_surveillance_ignores_quiet_stops():
    base, latest = np.full(50, 1000.0), np.full(50, 1000.0) * np.linspace(0.99, 1.01, 50)
    base[0], latest[0] = 20, 200                    # 10x but tiny: below the busy-stop floor
    s = surveillance(_volume(base, latest), "2026-08", "2026-07")
    assert "00000" not in set(s.stop)


def test_night_window_wraps_midnight(tmp_path, monkeypatch):
    import gzip
    import priority
    d = tmp_path / "speedbands"
    d.mkdir()
    # 26 Sep 2026 is a Saturday: its 08:00 snapshot must stay out of the weekday AM peak.
    for stamp in ["20260926_0800", "20260928_2330", "20260929_0300", "20260929_0800", "20260929_1400"]:
        with gzip.open(d / f"sb_{stamp}.csv.gz", "wt") as z:
            z.write("LinkID,SpeedBand,MinimumSpeed,MaximumSpeed\n1,8,70,999\n2,3,20,29\n")
    monkeypatch.setattr(priority, "RAW", tmp_path)
    night = load_snapshots(priority.NIGHT, False)
    am = load_snapshots(priority.AM, True)
    assert sorted(night.snapshot.dt.hour.unique()) == [3, 23]
    assert list(am.snapshot.dt.hour.unique()) == [8]
    assert list(am.snapshot.dt.day.unique()) == [29]
    assert set(night.kmh) == {75.0, 25.0}


def test_surveillance_needs_a_practical_effect():
    base = np.full(100, 1000.0) * np.linspace(0.999, 1.001, 100)
    latest = base.copy()
    latest[3] = base[3] * 1.10                      # huge z-score on a flat network, but only +10 %
    s = surveillance(_volume(base, latest), "2026-08", "2026-07")
    row = s.set_index("stop").loc["00003"]
    assert abs(row.robust_z) > 3.5 and row.flag == ""


def test_surveillance_is_robust_to_many_outliers():
    # 15 of 100 stops triple (e.g. a new estate opens). A standard deviation would be inflated by them and mask
    # them; the median absolute deviation is not.
    rng = np.random.default_rng(1)
    base = rng.uniform(500, 5000, 100)
    latest = base * rng.normal(1.0, 0.03, 100)
    latest[:15] = base[:15] * 3
    s = surveillance(_volume(base, latest), "2026-08", "2026-07")
    assert (s.flag == "surge").sum() == 15


def test_robust_z_is_centred_on_the_network_shift():
    rng = np.random.default_rng(2)
    base = rng.uniform(500, 5000, 100)
    latest = base * 1.4 * rng.normal(1.0, 0.03, 100)  # every stop +40 %
    s = surveillance(_volume(base, latest), "2026-08", "2026-07")
    assert s.robust_z.abs().median() < 1.5 and (s.flag == "").all()


def test_median_baseline_ignores_one_unusual_month():
    # Feb, Jun, Aug equal; July doubled (as at the polytechnics). Against July the stop "drops" 50 %;
    # against the median of Feb/Jun/Jul it has not changed.
    rows = []
    for stop in range(60):
        base = 1000.0 + stop
        for ym, k in (("2026-02", 1.0), ("2026-06", 1.0), ("2026-07", 2.0 if stop == 0 else 1.0), ("2026-08", 1.0)):
            rows.append({"stop": f"{stop:05d}", "YEAR_MONTH": ym, "DAY_TYPE": "WEEKDAY", "tap_in_per_day": base * k * (1 + stop % 7 / 1000)})
    v = pd.DataFrame(rows)
    single = surveillance(v, "2026-08", "2026-07").set_index("stop")
    median = surveillance(v, "2026-08", ["2026-02", "2026-06", "2026-07"]).set_index("stop")
    assert single.loc["00000", "flag"] == "drop"
    assert median.loc["00000", "flag"] == ""


def _row(**kw):
    base = dict(flag="surge", Description="Blk 1", RoadName="Some Rd", services_added="", services_removed="",
                near_campus=False, **{"wd_2026-02": 1000.0, "wd_2026-06": 1000.0, "wd_2026-07": 1000.0, "wd_2026-08": 1500.0})
    base.update(kw)
    return pd.Series(base)


@pytest.mark.parametrize("kw, expected", [
    (dict(flag="drop", services_removed="243W 258 502", services_added="181A"), "services withdrawn or rerouted away"),
    (dict(services_added="649", services_removed="982E"), "services added or rerouted here"),
    (dict(flag="drop", services_added="965"), "service change (direction unclear)"),
    (dict(Description="Lee Wee Nam Lib", RoadName="Nanyang Dr"), "academic term"),
    (dict(near_campus=True, **{"wd_2026-06": 500.0, "wd_2026-07": 600.0}), "academic term (within 600 m of a campus)"),
    (dict(**{"wd_2026-06": 700.0, "wd_2026-08": 1100.0}), "school-term seasonality (June holidays in the baseline)"),
    (dict(**{"wd_2026-07": 1500.0}), "sustained step up since July"),
    (dict(), "unexplained"),
    # boundaries: more services added than removed is not a withdrawal
    (dict(flag="drop", services_removed="965T", services_added="965 965A"), "service change (direction unclear)"),
    # low June but August far above February is not the school-term shape
    (dict(**{"wd_2026-06": 700.0, "wd_2026-08": 1600.0}), "unexplained"),
    # near a campus but without the term shape (June/July close to August)
    (dict(near_campus=True, **{"wd_2026-06": 1300.0, "wd_2026-07": 1300.0}), "unexplained"),
])
def test_classify(kw, expected):
    from anomalies import classify
    assert classify(_row(**kw)) == expected


def test_renumbered_services_are_not_changes():
    from anomalies import service_diff
    assert service_diff({"982E", "174"}, {"649", "174"}) == ([], [])
    assert service_diff({"982E"}, {"649", "684"}) == (["684"], [])


@pytest.mark.parametrize("tags, ok", [
    ({"highway": "footway"}, True), ({"highway": "residential"}, True), ({"highway": "primary"}, True),
    ({"highway": "steps"}, True), ({"highway": "motorway"}, False), ({"highway": "motorway_link"}, False),
    ({"highway": "cycleway"}, False), ({"highway": "construction"}, False), ({"highway": "footway", "foot": "no"}, False),
    ({"highway": "service", "service": "private"}, False), ({"highway": "pedestrian", "area": "yes"}, False),
    ({"highway": "service", "access": "private"}, False), ({"building": "yes"}, False),
])
def test_walkable_filter(tags, ok):
    from walk_network import walkable
    assert walkable(tags) is ok
