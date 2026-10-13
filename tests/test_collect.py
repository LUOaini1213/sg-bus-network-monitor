"""Collector logic that runs unattended for weeks: paging, atomic snapshot files, schedule ticks, incident log."""
import datetime as dt
import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import collect  # noqa: E402
import speed_cache  # noqa: E402


class FakeSession:
    """Serves `total` rows in pages of 500."""

    def __init__(self, total):
        self.total, self.calls = total, []

    def get(self, url, params, headers, timeout):
        skip = params["$skip"]
        self.calls.append(skip)
        # Like DataMall: short page at the end, empty page exactly at the end. Past the end this fake returns
        # junk, so a collector that keeps reading after the first short page is caught.
        n = min(500, self.total - skip) if skip <= self.total else (500 if skip < self.total + 5000 else 0)
        base = skip if skip <= self.total else 10 ** 6
        rows = [{"LinkID": base + i, "SpeedBand": 3, "MinimumSpeed": 20, "MaximumSpeed": 29} for i in range(n)]

        class R:
            status_code, text = 200, ""

            def raise_for_status(self):
                pass

            def json(self):
                return {"value": rows}
        return R()


@pytest.mark.parametrize("total, workers", [(1120, 4), (1120, 1), (1000, 2), (499, 3)])
def test_fetch_all_stops_at_the_first_short_page(total, workers):
    s = FakeSession(total)
    rows = collect.fetch_all(s, "k", "v4/TrafficSpeedBands", workers=workers)
    assert len(rows) == total
    assert [r["LinkID"] for r in rows] == list(range(total))


def test_write_snapshot_is_atomic_and_matches_the_sampler_format(tmp_path):
    rows = [{"LinkID": 7, "SpeedBand": 2, "MinimumSpeed": 10, "MaximumSpeed": 19, "RoadName": "X"}]
    path = collect.write_snapshot(rows, dt.datetime(2026, 10, 14, 8, 5), directory=tmp_path)
    assert path.name == "sb_20261014_0805.csv.gz"
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temporary file left behind
    with gzip.open(path, "rt") as z:
        d = pd.read_csv(z)
    assert d.columns.tolist() == ["LinkID", "SpeedBand", "MinimumSpeed", "MaximumSpeed"]
    assert d.iloc[0].tolist() == [7, 2, 10, 19]
    assert speed_cache.snapshot_time(path) == dt.datetime(2026, 10, 14, 8, 5)


def test_incident_polls_append_one_line_each(tmp_path):
    t = dt.datetime(2026, 10, 14, 8, 2)
    collect.append_incidents([{"Type": "Accident", "Message": "a"}], t, directory=tmp_path)
    collect.append_incidents([], t + dt.timedelta(minutes=2), directory=tmp_path)
    lines = (tmp_path / "inc_20261014.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["n"] for x in lines] == [1, 0]
    assert json.loads(lines[1])["polled_at"] == "2026-10-14T08:04:00"


@pytest.mark.parametrize("now, every, expected", [
    (dt.datetime(2026, 10, 14, 8, 3, 10), 300, dt.datetime(2026, 10, 14, 8, 5)),
    (dt.datetime(2026, 10, 14, 8, 5, 0), 300, dt.datetime(2026, 10, 14, 8, 10)),
    (dt.datetime(2026, 10, 14, 8, 5, 0), 120, dt.datetime(2026, 10, 14, 8, 6)),
])
def test_next_tick_is_the_next_clock_multiple(now, every, expected):
    assert collect.next_tick(now, every) == expected


def test_band_midpoints_and_missing_bands():
    v = speed_cache.band_kmh([1, 3, 8, 0], [0, 20, 70, np.nan], [9, 29, 999, np.nan])
    np.testing.assert_allclose(v, [5.0, 25.0, 75.0, np.nan], equal_nan=True)


def test_speed_cache_aligns_links_and_rebuilds_only_changed_days(tmp_path):
    raw, cache = tmp_path / "raw", tmp_path / "cache"
    raw.mkdir()

    def snap(when, rows):
        collect.write_snapshot([{"LinkID": l, "SpeedBand": b, "MinimumSpeed": 10 * (b - 1), "MaximumSpeed": 10 * b - 1}
                                for l, b in rows], when, directory=raw)

    snap(dt.datetime(2026, 10, 14, 8, 0), [(30, 2), (10, 4)])           # unordered, link 20 missing
    snap(dt.datetime(2026, 10, 15, 8, 0), [(99, 5), (10, 1), (20, 3)])  # link 99 is not watched
    links = np.array([10, 20, 30])
    files = speed_cache.build(raw, cache, links, verbose=False)
    t, li, k = speed_cache.load(cache=cache)
    assert li.tolist() == [10, 20, 30]
    np.testing.assert_allclose(k, [[35.0, np.nan, 15.0], [5.0, 25.0, np.nan]], equal_nan=True)
    assert [str(x) for x in t] == ["2026-10-14T08:00", "2026-10-15T08:00"]
    first = {f.name: f.stat().st_mtime_ns for f in files}
    snap(dt.datetime(2026, 10, 15, 8, 5), [(10, 2)])
    files = speed_cache.build(raw, cache, links, verbose=False)
    assert files[0].stat().st_mtime_ns == first["20261014.npz"]
    t, _, k = speed_cache.load(cache=cache, links=[30, 10])
    assert len(t) == 3
    np.testing.assert_allclose(k[:, 1], [35.0, 5.0, 15.0])


def test_quota_fault_is_not_retried():
    class Quota:
        calls = 0

        def get(self, url, params, headers, timeout):
            Quota.calls += 1

            class R:
                status_code = 500
                text = '{"fault":{"faultstring":"Rate limit quota violation. Quota limit  exceeded."}}'

                def raise_for_status(self):
                    raise collect.requests.HTTPError("500")
            return R()
    with pytest.raises(collect.QuotaExceeded):
        collect.get_page(Quota(), "k", "v4/TrafficSpeedBands", 0)
    assert Quota.calls == 1
