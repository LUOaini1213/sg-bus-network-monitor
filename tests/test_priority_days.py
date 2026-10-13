"""Multi-morning bus-priority screening: same per-snapshot rule as priority.link_speed, then per-morning medians."""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import priority  # noqa: E402
import priority_days as pdays  # noqa: E402

KEY = ["from_stop", "to_stop"]


def corridor(rows):
    return pd.DataFrame(rows).assign(services=3, AM_Peak_bph=60.0)


def matched_two_links():
    # Link A (0.4 km) has segments 1 and 2; link B (0.2 km) has segment 3.
    return pd.DataFrame({"from_stop": ["A", "A", "B"], "to_stop": ["A2", "A2", "B2"], "LinkID": [1, 2, 3],
                         "seg_m": [100.0, 300.0, 200.0], "link_km": [0.4, 0.4, 0.2]})


def test_matrix_speeds_equal_the_reference_link_speed():
    rng = np.random.default_rng(7)
    m = matched_two_links()
    links = np.array([1, 2, 3])
    times = [dt.datetime(2026, 9, 28, 8) + dt.timedelta(minutes=5 * i) for i in range(6)]
    kmh = rng.choice([5.0, 15.0, 25.0, 45.0, np.nan, 0.0], size=(6, 3), p=[.2, .2, .2, .2, .1, .1])
    long = pd.DataFrame([{"LinkID": l, "kmh": kmh[i, j], "snapshot": t} for i, t in enumerate(times)
                         for j, l in enumerate(links)])
    ref, _ = priority.link_speed(m, long, min_snapshots=1)
    w, index, km = pdays.link_matrix(m, links)
    sp = pdays.snapshot_link_speeds(kmh, w, km)
    for j, key in enumerate(index):
        expected = pd.Series([np.nan] * 6)
        x = m.merge(long, on="LinkID")
        x = x[(x.from_stop == key[0]) & np.isfinite(x.kmh) & (x.kmh > 0)]
        for i, t in enumerate(times):
            s = x[x.snapshot == t]
            if s.seg_m.sum() >= 0.5 * s.link_km.iloc[0] * 1000 if len(s) else False:
                expected[i] = (s.kmh * s.seg_m).sum() / s.seg_m.sum()
        np.testing.assert_allclose(sp[:, j], expected.to_numpy(), equal_nan=True)
        if np.isfinite(sp[:, j]).any():
            assert np.nanmedian(sp[:, j]) == pytest.approx(ref.loc[key])


def test_half_coverage_is_required_per_snapshot():
    m = matched_two_links()
    w, index, km = pdays.link_matrix(m, np.array([1, 2, 3]))
    # Only segment 1 (100 m of 400 m) is valid for link A: below half, so no speed.
    sp = pdays.snapshot_link_speeds(np.array([[20.0, np.nan, 30.0]]), w, km)
    assert np.isnan(sp[0, 0]) and sp[0, 1] == pytest.approx(30.0)
    # Segment 2 alone (300 m of 400 m) is enough.
    sp = pdays.snapshot_link_speeds(np.array([[np.nan, 20.0, 30.0]]), w, km)
    assert sp[0, 0] == pytest.approx(20.0)


def test_night_window_wraps_past_midnight_and_am_window_is_inclusive():
    t = np.array(["2026-10-14T21:59", "2026-10-14T22:00", "2026-10-15T03:00", "2026-10-15T05:00",
                  "2026-10-15T05:01", "2026-10-15T07:30", "2026-10-15T09:30", "2026-10-15T09:31"],
                 dtype="datetime64[m]")
    assert pdays.in_window(t, priority.NIGHT).tolist() == [False, True, True, True, False, False, False, False]
    assert pdays.in_window(t, priority.AM).tolist() == [False, False, False, False, False, True, True, False]


def am_times(day, n, start=dt.time(7, 30), step=5):
    base = dt.datetime.combine(day, start)
    return [base + dt.timedelta(minutes=step * i) for i in range(n)]


def test_mornings_exclude_weekends_holidays_and_thin_mornings():
    times = (am_times(dt.date(2026, 10, 14), 4)     # Wednesday, 4 snapshots: kept
             + am_times(dt.date(2026, 10, 15), 3)   # Thursday, 3 snapshots: too few
             + am_times(dt.date(2026, 10, 17), 6)   # Saturday
             + am_times(dt.date(2026, 11, 9), 6)    # Monday, Deepavali holiday (observed)
             + am_times(dt.date(2026, 10, 16), 4, start=dt.time(9, 15)))  # Friday, only 4 within 07:30-09:30? 9:15..9:30
    t = np.array(times, dtype="datetime64[m]")
    got = pdays.mornings(t)
    assert dt.date(2026, 10, 14) in got
    assert dt.date(2026, 10, 15) not in got
    assert dt.date(2026, 10, 17) not in got
    assert dt.date(2026, 11, 9) not in got
    assert dt.date(2026, 10, 16) in got  # 09:15, 09:20, 09:25, 09:30 are all inside the window


def build(days_speeds, night=40.0, link_km=1.0):
    """One bus link, one segment. days_speeds: {date: [speeds of AM snapshots]}; 4 night snapshots at `night`."""
    m = pd.DataFrame({"from_stop": ["A"], "to_stop": ["B"], "LinkID": [1], "seg_m": [link_km * 1000],
                      "link_km": [link_km]})
    cl = corridor([{"from_stop": "A", "to_stop": "B", "link_km": link_km, "match_ratio": 1.0}])
    times, speeds = [], []
    for d, vs in days_speeds.items():
        times += am_times(d, len(vs))
        speeds += vs
    nb = dt.datetime(2026, 10, 13, 23)
    times += [nb + dt.timedelta(minutes=10 * i) for i in range(4)]
    speeds += [night] * 4
    return m, cl, np.array(times, dtype="datetime64[m]"), np.array(speeds, dtype=float)[:, None], np.array([1])


D1, D2, D3 = dt.date(2026, 10, 14), dt.date(2026, 10, 15), dt.date(2026, 10, 16)


def test_combined_peak_is_median_of_daily_medians_not_pooled_snapshots():
    # Pooled median of the 12 snapshots would be 30; the median of the daily medians (10, 30) is 20.
    m, cl, t, k, links = build({D1: [10.0] * 4, D2: [30.0] * 8})
    ranked, by_day, _, meta = pdays.screen(m, cl, t, k, links)
    assert ranked.v_peak.iloc[0] == pytest.approx(20.0)
    assert ranked.bus_h_lost_per_h.iloc[0] == pytest.approx(60 * 1.0 * (1 / 20 - 1 / 40))
    assert sorted(by_day.morning) == [str(D1), str(D2)]
    assert [x["n_snapshots"] for x in meta["mornings"]] == [4, 8]


def test_median_not_mean_across_mornings():
    m, cl, t, k, links = build({D1: [10.0] * 4, D2: [10.0] * 4, D3: [40.0] * 4})
    ranked, *_ = pdays.screen(m, cl, t, k, links)
    assert ranked.v_peak.iloc[0] == pytest.approx(10.0)


def test_link_needs_speeds_on_half_the_mornings():
    # Link observed (4 covered snapshots) on 1 of 3 mornings: not ranked.
    m, cl, t, k, links = build({D1: [10.0] * 4, D2: [np.nan] * 4, D3: [np.nan] * 4})
    ranked, by_day, _, meta = pdays.screen(m, cl, t, k, links)
    assert meta["min_mornings_per_link"] == 2
    assert ranked.empty
    assert by_day.morning.tolist() == [str(D1)]  # the morning-level result still exists


def test_daily_floor_counts_only_that_mornings_snapshots():
    # 3 covered snapshots on each of two mornings: 6 in total, but neither morning reaches 4.
    m, cl, t, k, links = build({D1: [10.0, 10.0, 10.0, np.nan], D2: [12.0, 12.0, 12.0, np.nan]})
    ranked, by_day, *_ = pdays.screen(m, cl, t, k, links)
    assert ranked.empty and by_day.empty


def test_topk_overlap_and_kendall_by_hand():
    idx = [f"l{i}" for i in range(6)]
    a = pd.Series([6, 5, 4, 3, 2, 1], index=idx, dtype=float)
    b = pd.Series([1, 5, 6, 3, 2, 4], index=idx, dtype=float)
    # top-3 of a = l0, l1, l2; top-3 of b = l2, l1, l5 -> 2 shared of 3.
    assert pdays.topk_overlap(a, b, k=3) == pytest.approx(2 / 3)
    from scipy.stats import kendalltau
    assert pdays.kendall(a, b) == pytest.approx(kendalltau(a, b).statistic)
    # Links scored on only one side are left out before the top-k is taken.
    b2 = b.copy()
    b2["l0"] = np.nan
    assert pdays.topk_overlap(a, b2, k=3) == pytest.approx(2 / 3)  # a's top-3 is now l1, l2, l3
    assert np.isnan(pdays.topk_overlap(a.iloc[:2], b.iloc[:2], k=3))


def test_stability_counts_mornings_in_daily_top():
    idx = pd.Index(["x", "y", "z"])
    per_day = {D1: pd.Series([3.0, 2.0, 1.0], index=idx), D2: pd.Series([1.0, 3.0, 2.0], index=idx),
               D3: pd.Series([3.0, 1.0, 2.0], index=idx)}
    combined = pd.Series([3.0, 2.0, 2.0], index=idx)
    stab, in_top = pdays.stability(per_day, combined, k=1)
    assert in_top.to_dict() == {"x": 2, "y": 1, "z": 0}
    assert stab["summary"]["mornings"] == 3
    assert len(stab["pairs"]) == 3
    assert stab["summary"]["combined_top1_in_daily_top1_on_half_or_more_mornings"] == 1


def test_daily_peak_uses_only_the_am_window_of_that_morning():
    # Four 07:30-07:45 snapshots at 10 km/h and four same-day 12:00 snapshots at 40 km/h: the peak is 10.
    m, cl, t, k, links = build({D1: [10.0] * 4})
    noon = np.array([dt.datetime(2026, 10, 14, 12, 0) + dt.timedelta(minutes=5 * i) for i in range(4)],
                    dtype="datetime64[m]")
    t = np.r_[t, noon]
    k = np.r_[k, np.full((4, 1), 40.0)]
    ranked, by_day, _, meta = pdays.screen(m, cl, t, k, links)
    assert by_day.v_peak.tolist() == [10.0]
    assert meta["mornings"] == [{"date": str(D1), "n_snapshots": 4}]
