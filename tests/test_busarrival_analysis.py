"""Arrival inference, ETA accuracy, headway regularity, bunching and data quality on hand-built answers."""
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import busarrival as ba  # noqa: E402
import busarrival_analysis as an  # noqa: E402

SGT = an.SGT
BASE = dt.datetime(2026, 10, 16, 8, 0, tzinfo=SGT)


def at(minutes):
    return BASE + dt.timedelta(minutes=minutes)


def bus(eta_min, monitored=1, load="SEA"):
    return {"EstimatedArrival": at(eta_min).isoformat(), "Monitored": monitored, "Load": load, "Type": "DD",
            "Latitude": "1.30", "Longitude": "103.9", "VisitNumber": "1"}


EMPTY = {"EstimatedArrival": "", "Monitored": 0, "Load": "", "Type": "", "Latitude": "", "Longitude": ""}


def answer(t_min, etas, service="10", stop="92041", monitored=1, others=()):
    """One stop answer at minute t_min listing `etas` (absolute minutes) for one service (None: not listed)."""
    services = []
    if etas is not None:
        s = {"ServiceNo": service}
        for i, slot in enumerate(an.SLOTS):
            s[slot] = bus(etas[i], monitored) if i < len(etas) else dict(EMPTY)
        services.append(s)
    services += list(others)
    return {"stop": stop, "t": at(t_min).isoformat(timespec="milliseconds"), "status": "ok", "http": 200,
            "services": services}


def arrivals(lines):
    return an.track(an.answers_by_group(lines)[("92041", "10")])


def test_bus_that_leaves_the_list_arrived_between_the_two_answers():
    lines = [answer(0, [3, 13]), answer(2, [3.2, 13]), answer(4, [13.5, 23]), answer(6, [13.6, 23]),
             answer(8, [13.4, 23]), answer(10, [13.4, 23]), answer(12, [13.4, 23]), answer(14, [23, 33])]
    tracks, events = arrivals(lines)
    a, b = tracks[0], tracks[1]
    assert (a["how"], a["lo"], a["hi"]) == ("gone", at(2), at(4))
    assert (b["how"], b["lo"], b["hi"]) == ("gone", at(12), at(14))
    lines += [answer(m, [23, 33]) for m in range(16, 25, 2)]   # observed to 08:24, so the 08:03 bus counts
    preds, heads, _ = an.infer(lines)
    pa = preds[preds.track == 0].sort_values("t")
    assert pa.horizon_min.round(6).tolist() == [3.0, 1.2]
    assert pa.error_s.round(6).tolist() == [0.0, -12.0]   # observed 08:03:00; predicted 08:03:00 and 08:03:12
    assert (pa.resolution_s == 60).all()
    assert heads.headway_min.round(6).tolist() == [10.0, 10.0]   # 08:03, 08:13, 08:23


@pytest.mark.parametrize("monitored, how, mid", [(1, "zero", 5.0), (0, "gone", 9.0)])
def test_eta_crossing_zero_counts_only_for_monitored_buses(monitored, how, mid):
    lines = [answer(m, e, monitored=monitored) for m, e in
             ((0, [5, 20]), (4, [5, 20]), (6, [5.5, 20]), (8, [5.5, 20]), (10, [20]))]
    tracks, events = arrivals(lines)
    t = tracks[0]
    assert t["how"] == how
    assert t["lo"] + (t["hi"] - t["lo"]) / 2 == at(mid)
    assert [e for e in events if e[0] == "arrival"] == [("arrival", at(mid))]


def test_bus_dropped_far_from_the_stop_is_lost_and_breaks_the_headways():
    lines = [answer(0, [1, 15]), answer(2, []), answer(4, [5, 16]), answer(6, [5.5, 16]), answer(8, [16, 26]),
             answer(10, [16, 26]), answer(12, [16, 26]), answer(14, [16.2, 26]), answer(16, [15.9, 26])]
    tracks, events = arrivals(lines)
    assert [t["how"] for t in tracks] == ["gone", "lost", "zero", "zero", "open"]
    preds, heads, trks = an.infer(lines)
    # arrivals at 08:01, 08:05 and 08:15: the bus lost at 08:02 breaks the sequence before the 08:05 one
    assert heads.headway_min.round(6).tolist() == [10.0]
    assert trks.how.value_counts().to_dict()["lost"] == 1


def test_service_missing_from_an_answer_means_its_buses_left():
    other = {"ServiceNo": "99", "NextBus": bus(30)}
    lines = [answer(0, [1.5]), answer(2, None, others=[other])]
    tracks, _ = arrivals(lines)
    assert tracks[0]["how"] == "gone" and tracks[0]["hi"] == at(2)


def test_a_blank_answer_is_missing_not_every_bus_leaving():
    blank = {"stop": "92041", "t": at(4).isoformat(), "status": "ok", "http": 200, "services": []}
    lines = [answer(0, [3, 13]), answer(2, [3.2, 13.1]), blank, answer(6, [13, 23])]
    tracks, _ = arrivals(lines)
    assert [(t["how"], t["lo"], t["hi"]) for t in tracks[:2]] == [("gone", at(2), at(6)), ("open", None, None)]
    assert an.quality(lines, an.infer(lines)[2], an.infer(lines)[0])["blank_answers"] == 1


def test_two_bunched_buses_leaving_together_give_a_zero_headway():
    lines = [answer(0, [1.5, 1.8, 12])] + [answer(m, [12.1, 22]) for m in range(2, 13, 2)] + [answer(14, [22, 32])]
    preds, heads, _ = an.infer(lines)
    assert heads.headway_min.round(6).tolist() == [0.0, 12.0]
    assert an.bunching(heads, {}).bunched.tolist() == [True, False]


@pytest.mark.parametrize("old, new, t, s", [
    ([3.0], [3.5], 2, 0),            # same bus, small change: retiring it and aligning nothing costs NO_MATCH_S
    ([3.0, 3.5, 12], [3.4, 12.2], 2, 1),
    ([1.5, 1.8, 12], [12.1], 2, 2),
    ([6.0, 12], [12.5], 2, 0),       # retiring a bus still 4 minutes out is implausible
    ([2.5, 12], [], 2, 2),
    ([16, 26], [26], 8, 0),          # a tie (600 s either way) keeps the smaller shift
])
def test_best_shift(old, new, t, s):
    assert an.best_shift([{"eta": at(x)} for x in old], [{"eta": at(x)} for x in new], at(t)) == s


def test_a_gap_in_the_answers_closes_tracks_without_arrivals():
    lines = [answer(0, [3, 13]), answer(2, [3, 13]), answer(10, [13, 23]), answer(12, [13, 23]),
             answer(14, [23])]
    tracks, events = arrivals(lines)
    assert tracks[0]["how"] == "gap"            # the 08:03 bus left during the 8-minute gap: unknown when
    preds, heads, _ = an.infer(lines)
    assert heads.empty


def test_buses_due_in_the_last_ten_minutes_are_not_scored():
    lines = [answer(0, [3, 13]), answer(2, [3.2, 13]), answer(4, [13.5, 23]), answer(6, [13.6, 23]),
             answer(8, [13.4, 23]), answer(10, [13.4, 23]), answer(12, [13.4, 23]), answer(14, [23, 33])]
    preds, _, _ = an.infer(lines)   # last answer 08:14: only ETAs up to 08:04 are scored
    assert sorted(preds.eta.unique()) == [at(3), at(3.2)]


def test_horizon_bins_are_left_closed():
    p = pd.DataFrame({"stop": "s", "service": "1", "track": [0, 1, 2, 3, 4], "monitored": 1,
                      "horizon_min": [1.99, 2.0, 5.0, 19.9, -0.5], "error_s": [30, -90, 200, 400, 0],
                      "resolution_s": 60.0})
    acc = an.eta_accuracy(p).set_index("horizon")
    assert acc.n.to_dict() == {"0-2": 1, "2-5": 1, "5-10": 1, "10-20": 1}
    assert acc.within_120.to_dict() == {"0-2": 1.0, "2-5": 1.0, "5-10": 0.0, "10-20": 0.0}
    assert acc.bias_s["2-5"] == -90 and acc.mae_s["2-5"] == 90


def test_regularity_uses_the_sample_standard_deviation():
    h = pd.DataFrame({"stop": "s", "service": ["1"] * 4 + ["2"] * 4 + ["3"] * 3,
                      "headway_min": [10, 10, 10, 10, 5, 15, 5, 15, 1, 2, 3]})
    r = an.regularity(h).set_index("service")
    assert r.index.tolist() == ["1", "2"]       # three headways are too few
    assert r.cv["1"] == 0
    assert r.cv["2"] == pytest.approx(np.sqrt(100 / 3) / 10)


def test_bunching_threshold_and_reference():
    h = pd.DataFrame({"stop": "s", "service": ["1", "1", "2", "2"], "arrival": [at(m) for m in (0, 1, 2, 3)],
                      "headway_min": [2.4, 2.5, 1.0, 8.0]})   # 08:00-08:03 is the AM peak
    sched = {("s", "1"): {"am_peak": 10.0, "am_offpeak": 20.0, "pm_peak": None, "pm_offpeak": None}}
    b = an.bunching(h, sched)
    assert b.reference.tolist() == ["scheduled", "scheduled", "median", "median"]
    assert b.reference_min.tolist() == [10.0, 10.0, 4.5, 4.5]
    assert b.bunched.tolist() == [True, False, True, False]   # under 25%: 2.4 < 2.5; 2.5 is not; 1.0 < 1.125


@pytest.mark.parametrize("hm, period", [((6, 30), "am_peak"), ((8, 30), "am_peak"), ((8, 31), "am_offpeak"),
                                        ((17, 0), "pm_peak"), ((19, 0), "pm_peak"), ((19, 1), "pm_offpeak"),
                                        ((6, 0), "pm_offpeak")])
def test_frequency_periods(hm, period):
    assert an.period_of(dt.datetime(2026, 10, 16, *hm, tzinfo=SGT)) == period


def test_scheduled_headways_come_from_the_reference_table():
    s = an.scheduled_headways()
    assert s[("08041", "124")]["am_peak"] == 7.5   # AM_Peak_Freq "07-08"


def test_quality_report_and_full_run(tmp_path):
    lines = [answer(0, [3, 13]), answer(2, [3.2, 13]), answer(4, [13.5, 23]), answer(6, [13.6, 23])]
    lines[1]["services"][0]["NextBus2"]["Load"] = "LSD"
    lines[2]["services"][0]["NextBus2"]["Monitored"] = 0
    lines.append({"stop": "92041", "t": None, "status": "skipped", "http": None, "services": []})
    ba.write_sweep(lines, dt.datetime(2026, 10, 16, 8, 0), tmp_path / "2026-10-16")
    s, q = an.run(tmp_path, out=tmp_path / "out")
    assert q["answer_status"] == {"ok": 4, "skipped": 1}
    assert q["listed_buses"] == 8 and q["load_share"] == {"SEA": 7 / 8, "LSD": 1 / 8}
    assert q["monitored_share"] == 7 / 8 and q["nextbus_missing_eta_share"] == 0.0
    assert s["arrivals"]["gone"] == 1
    for f in ("busarrival_eta_accuracy.csv", "busarrival_headways.csv", "busarrival_bunching.csv",
              "busarrival_quality.json", "busarrival_summary.json"):
        assert (tmp_path / "out" / f).exists()
