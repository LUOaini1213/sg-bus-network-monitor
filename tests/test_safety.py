"""Safety brief: exact intervals, rate ratios, incident de-duplication and snapping to the road network."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safety  # noqa: E402


def test_garwood_interval_known_values():
    lo, hi = safety.poisson_ci([0, 10])
    assert lo[0] == 0.0 and hi[0] == pytest.approx(3.6889, abs=1e-4)
    assert lo[1] == pytest.approx(4.7954, abs=1e-4) and hi[1] == pytest.approx(18.3904, abs=1e-4)


def test_rate_ratio_interval_and_exposure_scaling():
    rr, lo, hi = safety.rate_ratio(10, 1.0, 10, 1.0)
    assert rr == pytest.approx(1.0)
    # Clopper-Pearson for 10 of 20 is (0.27196, 0.72804); as a ratio p / (1 - p).
    assert lo == pytest.approx(0.27196 / 0.72804, rel=1e-3)
    assert hi == pytest.approx(0.72804 / 0.27196, rel=1e-3)
    rr2, lo2, hi2 = safety.rate_ratio(10, 2.0, 10, 1.0)  # same count over twice the exposure: half the rate
    assert (rr2, lo2, hi2) == pytest.approx((0.5, lo / 2, hi / 2))


def test_incidents_are_counted_once_across_polls(tmp_path):
    a = {"Type": "Accident", "Message": "(14/10)08:01 Accident on X Rd", "Latitude": 1.3, "Longitude": 103.8}
    b = {"Type": "Accident", "Message": "(14/10)08:05 Accident on Y Rd", "Latitude": 1.31, "Longitude": 103.81}
    polls = [{"polled_at": "2026-10-14T08:02:00", "n": 1, "value": [a]},
             {"polled_at": "2026-10-14T08:04:00", "n": 1, "value": [a]},
             {"polled_at": "2026-10-14T08:06:00", "n": 2, "value": [a, b]}]
    (tmp_path / "inc_20261014.jsonl").write_text("\n".join(json.dumps(p) for p in polls) + "\n", encoding="utf-8")
    inc, n = safety.load_incidents(tmp_path)
    assert n == 3 and len(inc) == 2
    first = inc[inc.message == a["Message"]].iloc[0]
    assert (first.first_seen, first.last_seen, first.polls) == ("2026-10-14T08:02:00", "2026-10-14T08:06:00", 3)


def test_snap_uses_distance_to_the_segment_not_its_ends():
    s = np.array([[0.0, 0.0], [0.0, 100.0]])
    e = np.array([[200.0, 0.0], [200.0, 100.0]])
    d, i = safety.snap(np.array([[100.0, 20.0], [100.0, 80.0], [220.0, -5.0]]), s, e)
    assert d[0] == pytest.approx(20.0) and i[0] == 0
    assert d[1] == pytest.approx(20.0) and i[1] == 1
    assert d[2] == pytest.approx(np.hypot(20, 5))  # past the end: distance to the end point


def test_bus_route_share_is_by_length_and_needs_a_match():
    # Two 1 km east-west segments 1 km apart, and a 3 km expressway; only the first carries buses.
    lat0, lon0 = 1.30, 103.80
    dlat, dlon = 1 / 111.32, 1 / 111.32
    links = pd.DataFrame({"LinkID": [1, 2, 3], "RoadCategory": [3, 3, 1],
                          "StartLat": [lat0, lat0 + dlat, lat0 + 3 * dlat], "StartLon": [lon0, lon0, lon0],
                          "EndLat": [lat0, lat0 + dlat, lat0 + 3 * dlat],
                          "EndLon": [lon0 + dlon, lon0 + dlon, lon0 + 3 * dlon]})
    inc = pd.DataFrame({"type": ["Accident"] * 4, "message": list("abcd"),
                        "lat": [lat0 + 0.0001, lat0 + dlat, lat0 + 3 * dlat, lat0 + 2 * dlat],
                        "lon": [lon0 + dlon / 2] * 4})
    out, by_type, base = safety.incidents_vs_network(inc, links, {1})
    assert base["bus_route_share_of_road_length"] == pytest.approx(1 / 5, abs=0.01)
    assert base["bus_route_share_of_non_expressway_length"] == pytest.approx(1 / 2, abs=0.01)
    assert out.matched.tolist() == [True, True, True, False]  # the last one is ~1 km from any road
    assert out.on_bus_route.tolist() == [True, False, False, False]
    row = by_type.iloc[0]
    assert (row.incidents, row.on_bus_route, row.non_expressway_incidents, row.non_expressway_on_bus_route) == \
        (3, 1, 2, 1)
