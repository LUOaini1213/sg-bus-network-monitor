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
