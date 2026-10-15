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


# ---------------------------------------------------------------- GitHub Actions collector: window, holidays, cron
UTC = dt.timezone.utc
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "collect.yml"


@pytest.mark.parametrize("now, expected", [
    (dt.datetime(2026, 10, 19, 6, 30), True),             # Monday, first pull
    (dt.datetime(2026, 10, 19, 6, 29), False),
    (dt.datetime(2026, 10, 23, 10, 0), True),             # Friday, last pull
    (dt.datetime(2026, 10, 23, 10, 20), True),            # a late start still belongs to the morning
    (dt.datetime(2026, 10, 23, 10, 21), False),
    (dt.datetime(2026, 10, 23, 13, 0), False),
    (dt.datetime(2026, 10, 24, 1, 10), True),             # night pulls run every night, weekends included
    (dt.datetime(2026, 11, 9, 3, 10), True),              # and on holidays
    (dt.datetime(2026, 10, 20, 22, 0), True),
    (dt.datetime(2026, 10, 20, 21, 59), False),
    (dt.datetime(2026, 10, 21, 5, 0), True),
    (dt.datetime(2026, 10, 21, 5, 1), False),
    (dt.datetime(2026, 10, 24, 17, 10, tzinfo=UTC), True),   # Sunday 01:10 SGT
    (dt.datetime(2026, 10, 24, 8, 0), False),             # Saturday
    (dt.datetime(2026, 10, 25, 8, 0), False),             # Sunday
    (dt.datetime(2026, 11, 9, 8, 0), False),              # Monday after Deepavali (holiday in lieu)
    (dt.datetime(2026, 12, 25, 8, 0), False),             # Christmas, a Friday
    (dt.datetime(2026, 10, 18, 22, 30, tzinfo=UTC), True),   # Sunday night UTC is Monday 06:30 SGT
    (dt.datetime(2026, 10, 19, 6, 30, tzinfo=UTC), False),   # 06:30 UTC is 14:30 SGT
    (dt.datetime(2026, 10, 23, 2, 0, tzinfo=UTC), True),     # Friday 10:00 SGT
    (dt.datetime(2026, 10, 23, 22, 30, tzinfo=UTC), False),  # Friday night UTC is Saturday morning SGT
    (dt.datetime(2026, 11, 8, 23, 0, tzinfo=UTC), False),    # 07:00 SGT on the 9 November holiday
])
def test_collection_window_is_weekday_mornings_in_singapore_time_without_holidays(now, expected):
    assert collect.collection_window(now)[0] is expected


def test_night_window_is_the_priority_screens_night_reference():
    import priority
    assert collect.NIGHT_WINDOW == priority.NIGHT


def test_collection_window_uses_the_given_holiday_list_and_warns_past_its_years():
    monday = dt.datetime(2026, 10, 19, 8, 0)
    assert collect.collection_window(monday, holidays={dt.date(2026, 10, 19)})[0] is False
    ok, reason = collect.collection_window(dt.datetime(2027, 1, 4, 8, 0))
    assert ok and "2027" in reason


def _cron_field(text, lo, hi):
    out = set()
    for part in text.split(","):
        rng, _, step = part.partition("/")
        a, b = (lo, hi) if rng == "*" else map(int, (rng.split("-") * 2)[:2])
        out |= set(range(a, b + 1, int(step or 1)))
    return out


def _cron_fires(expr, start, end):
    """UTC times between start and end matched by a 5-field cron with *, lists, ranges and steps (no dom/month)."""
    minute, hour, dom, month, dow = expr.split()
    assert dom == "*" and month == "*"
    mins, hours, days = _cron_field(minute, 0, 59), _cron_field(hour, 0, 23), _cron_field(dow, 0, 6)
    t, out = start, set()
    while t < end:
        if t.minute in mins and t.hour in hours and (t.isoweekday() % 7) in days:
            out.add(t)
        t += dt.timedelta(minutes=1)
    return out


def test_workflow_cron_fires_on_weekday_mornings_every_15_minutes_and_twice_every_night():
    import re
    crons = re.findall(r'cron:\s*"([^"]+)"', WORKFLOW.read_text(encoding="utf-8"))
    start = dt.datetime(2026, 10, 18, tzinfo=UTC)  # a Sunday; one full week
    fires = set().union(*(_cron_fires(c, start, start + dt.timedelta(days=7)) for c in crons))
    sgt = sorted(f.astimezone(collect.SGT).replace(tzinfo=None) for f in fires)
    mornings = [dt.datetime(2026, 10, d, 6, 30) + dt.timedelta(minutes=15 * k)
                for d in range(19, 24) for k in range(15)]
    nights = [dt.datetime(2026, 10, d, h, 10) for d in range(19, 26) for h in (1, 3)]  # 17:10, 19:10 UTC
    assert sgt == sorted(mornings + nights)
    assert all(collect.collection_window(f)[0] for f in fires)
    assert "LTA_KEY: ${{ secrets.LTA_ACCOUNT_KEY }}" in WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------- one pull per run: quota backoff and manifest
class QuotaThenRows(FakeSession):
    """Answers the first `faults` calls with DataMall's quota fault, then serves pages like FakeSession."""

    def __init__(self, total, faults):
        super().__init__(total)
        self.faults = faults

    def get(self, url, params, headers, timeout):
        if self.faults:
            self.faults -= 1
            self.calls.append(params["$skip"])

            class R:
                status_code, text = 429, '{"fault":{"faultstring":"Rate limit quota violation."}}'

                def raise_for_status(self):
                    raise collect.requests.HTTPError("429")
            return R()
        return super().get(url, params, headers, timeout)


def test_quota_fault_waits_with_backoff_then_asks_for_the_same_page(monkeypatch):
    waits, retries = [], []
    monkeypatch.setattr(collect.time, "sleep", waits.append)
    monkeypatch.setattr(collect, "ECHO", True)
    s = QuotaThenRows(1120, faults=2)
    rows = collect.fetch_all(s, "k", "v4/TrafficSpeedBands", workers=1, backoff=(60, 120, 240), retries=retries)
    assert [r["LinkID"] for r in rows] == list(range(1120))
    assert waits == [60, 120] and retries == [0, 0]
    assert s.calls[:3] == [0, 0, 0]


def test_quota_fault_after_the_last_backoff_is_raised(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    monkeypatch.setattr(collect, "ECHO", True)
    with pytest.raises(collect.QuotaExceeded):
        collect.fetch_all(QuotaThenRows(10, faults=3), "k", "v4/TrafficSpeedBands", workers=1, backoff=(1, 2))


def test_once_writes_one_snapshot_one_incident_poll_and_manifests(tmp_path, monkeypatch):
    import hashlib

    class Session(FakeSession):
        def get(self, url, params, headers, timeout):
            r = super().get(url, params, headers, timeout)
            if url.endswith("TrafficIncidents"):
                r.json = lambda: {"value": [{"Type": "Accident", "Message": "x"}] if params["$skip"] == 0 else []}
            else:
                rows = r.json()["value"]
                for x in rows:
                    x.update(RoadName="R", RoadCategory=1, StartLon=103.8, StartLat=1.3, EndLon=103.81, EndLat=1.31)
                r.json = lambda: {"value": rows}
            return r
    monkeypatch.setenv("LTA_KEY", "secret-key")
    monkeypatch.setattr(collect.requests, "Session", lambda: Session(1200))
    monkeypatch.setattr(collect, "ECHO", True)
    stamp = dt.datetime(2026, 10, 19, 7, 15)
    assert collect.once(tmp_path, stamp, expected_links=1200) == 0
    day = tmp_path / "data" / "raw" / "speedbands" / "2026-10-19"
    snap = day / "sb_20261019_0715.csv.gz"
    m = pd.read_csv(day / "manifest.csv")
    assert m.columns.tolist() == collect.MANIFEST_COLUMNS
    assert m.loc[0, "rows"] == 1200 and m.loc[0, "status"] == "ok"
    assert m.loc[0, "sha256"] == hashlib.sha256(snap.read_bytes()).hexdigest()
    assert m.loc[0, "snapshot_sgt"] == "2026-10-19T07:15"
    inc = tmp_path / "data" / "raw" / "incidents" / "2026-10-19"
    with gzip.open(inc / "inc_20261019_0715.jsonl.gz", "rt") as z:
        assert json.loads(z.read())["n"] == 1
    assert pd.read_csv(inc / "manifest.csv").loc[0, "rows"] == 1
    links = tmp_path / "data" / "raw" / "speedbands" / "links" / "speedband_links.csv.gz"
    assert pd.read_csv(links).columns.tolist() == collect.LINK_COLUMNS
    before = links.stat().st_mtime_ns
    assert collect.once(tmp_path, stamp + dt.timedelta(minutes=15), expected_links=1200) == 0
    assert links.stat().st_mtime_ns == before  # unchanged geometry is not rewritten
    assert len(pd.read_csv(day / "manifest.csv")) == 2
    assert not any("secret-key" in p.read_bytes().decode("latin-1") for p in tmp_path.rglob("*") if p.is_file())


def test_default_quota_backoff_is_three_tries_of_the_same_page(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    monkeypatch.setattr(collect, "ECHO", True)
    s = QuotaThenRows(10, faults=2)  # third try answers
    assert len(collect.fetch_all(s, "k", "v4/TrafficSpeedBands", workers=1, backoff=collect.QUOTA_BACKOFF)) == 10
    assert s.calls[:3] == [0, 0, 0]
    s = QuotaThenRows(10, faults=3)  # three refusals: give up, no fourth call
    with pytest.raises(collect.QuotaExceeded):
        collect.fetch_all(s, "k", "v4/TrafficSpeedBands", workers=1, backoff=collect.QUOTA_BACKOFF)
    assert s.calls == [0, 0, 0]


def test_once_records_a_failed_pull_in_the_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LTA_KEY", "k")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    monkeypatch.setattr(collect.requests, "Session", lambda: QuotaThenRows(10, faults=10 ** 6))
    monkeypatch.setattr(collect, "ECHO", True)
    # out of quota after the backoff: logged as a warning, not counted, so the run exits 0
    assert collect.once(tmp_path, dt.datetime(2026, 10, 19, 8, 0), backoff=(1, 2)) == 0
    out = capsys.readouterr().out
    assert "::warning::speedbands pull" in out and "::error::" not in out
    m = pd.read_csv(tmp_path / "data" / "raw" / "speedbands" / "2026-10-19" / "manifest.csv")
    assert m.loc[0, "status"].startswith("failed: QuotaExceeded") and m.loc[0, "rows"] == 0
    assert m.loc[0, "quota_retries"] >= 2  # two waits on the first page; parallel pages may add their own
    assert not list((tmp_path / "data" / "raw" / "speedbands" / "2026-10-19").glob("sb_*"))


def test_missing_key_is_an_actions_error_annotation_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LTA_KEY", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(collect, "ECHO", True)
    with pytest.raises(SystemExit):
        collect.once(tmp_path, dt.datetime(2026, 10, 19, 8, 0))
    assert "::error::LTA_KEY is empty" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_a_failed_pull_that_is_not_the_quota_fails_the_run(tmp_path, monkeypatch, capsys):
    class Down(FakeSession):
        def get(self, url, params, headers, timeout):
            class R:
                status_code, text = 503, "Service Unavailable"

                def raise_for_status(self):
                    raise collect.requests.HTTPError("503")
            return R()
    monkeypatch.setenv("LTA_KEY", "k")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    monkeypatch.setattr(collect.requests, "Session", lambda: Down(10))
    monkeypatch.setattr(collect, "ECHO", True)
    assert collect.once(tmp_path, dt.datetime(2026, 10, 19, 8, 0)) == 2
    assert "::error::speedbands pull" in capsys.readouterr().out


def test_a_manual_run_always_pulls():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch" in text
    assert "collect.py gate ${{ github.event_name == 'workflow_dispatch' && '--force' || '' }}" in text
