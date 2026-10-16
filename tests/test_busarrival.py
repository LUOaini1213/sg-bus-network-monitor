"""Bus Arrival collector: window, pacing, quota backoff, files; and the arrival inference and metrics."""
import datetime as dt
import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import busarrival as ba  # noqa: E402

SGT = ba.SGT
KEY = "SECRET-KEY-123"


class Clock:
    """A fake clock: sleeping and each DataMall call move it forward."""

    def __init__(self, t):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += dt.timedelta(seconds=s)


class Session:
    """Answers each stop with one service; `quota_at` call numbers answer with DataMall's quota fault."""

    def __init__(self, clock, quota_at=(), status_at=None, latency=0.4):
        self.clock, self.quota_at, self.status_at, self.latency = clock, set(quota_at), status_at or {}, latency
        self.calls = []

    def get(self, url, params, headers, timeout):
        assert headers["AccountKey"] == KEY and "AccountKey" not in params
        self.calls.append((self.clock(), params["BusStopCode"]))
        self.clock.t += dt.timedelta(seconds=self.latency)
        n = len(self.calls)
        quota = n in self.quota_at
        status = 500 if quota else self.status_at.get(n, 200)

        class R:
            status_code = status
            text = '{"fault":{"faultstring":"Rate limit quota violation. Quota limit  exceeded."}}' if quota else ""

            def raise_for_status(self):
                if status >= 400:
                    import requests
                    raise requests.HTTPError(f"{status}", response=self)

            def json(self):
                return {"BusStopCode": params["BusStopCode"], "Services": [{"ServiceNo": "10", "NextBus": {}}]}
        return R()


def sgt(h, m, s=0, day=16):
    return dt.datetime(2026, 10, day, h, m, s, tzinfo=SGT)


@pytest.mark.parametrize("now, start, until", [
    (sgt(6, 0), sgt(6, 30), sgt(10, 0)),     # scheduled start: wait for 06:30
    (sgt(7, 12), sgt(7, 12), sgt(10, 0)),    # late start: poll from now
    (sgt(5, 40), None, None),                # too early
    (sgt(10, 0), None, None),                # closed
    (sgt(7, 0, day=17), None, None),         # Saturday
    (dt.datetime(2026, 11, 9, 7, 0, tzinfo=SGT), None, None),   # Deepavali holiday (Monday)
    (dt.datetime(2026, 10, 15, 22, 30, tzinfo=dt.timezone.utc), sgt(6, 30), sgt(10, 0)),  # UTC runner clock
])
def test_window_is_singapore_weekday_mornings(now, start, until):
    s, u, _ = ba.window(now)
    assert (s, u) == (start, until)


def test_sweep_spreads_calls_over_the_period_one_at_a_time(tmp_path):
    clock = Clock(sgt(7, 0))
    session = Session(clock)
    stops = [f"{i:05d}" for i in range(40)]
    n, calls, failed, quota = ba.poll(tmp_path, sgt(7, 6), KEY, stops, session=session, clock=clock,
                                      sleep=clock.sleep)
    assert (n, calls, failed, quota) == (3, 120, 0, 0)
    starts = [t for t, _ in session.calls]
    gaps = np.diff([(t - starts[0]).total_seconds() for t in starts])
    np.testing.assert_allclose(gaps, 3.0, atol=1e-6)   # 120 s / 40 stops, never a burst
    assert (starts[40] - starts[0]).total_seconds() == pytest.approx(120)
    assert [s for _, s in session.calls[:40]] == stops


def test_files_manifest_and_no_key_anywhere(tmp_path):
    clock = Clock(sgt(7, 0, 5))
    stops = ["92041", "08057"]
    ba.poll(tmp_path, sgt(7, 4), KEY, stops, session=Session(clock), clock=clock, sleep=clock.sleep)
    day = tmp_path / "data" / "raw" / "busarrival" / "2026-10-16"
    files = sorted(p.name for p in day.iterdir())
    assert files == ["ba_20261016_070005.jsonl.gz", "ba_20261016_070205.jsonl.gz", "manifest.csv"]
    with gzip.open(day / files[0], "rt", encoding="utf-8") as z:
        lines = [json.loads(x) for x in z]
    assert [x["stop"] for x in lines] == stops and lines[0]["status"] == "ok"
    assert lines[0]["t"].startswith("2026-10-16T07:00:05") and lines[0]["t"].endswith("+08:00")
    m = pd.read_csv(day / "manifest.csv")
    assert m.ok.tolist() == [2, 2] and m.services_listed.tolist() == [2, 2]
    import hashlib
    assert m.sha256[0] == hashlib.sha256((day / files[0]).read_bytes()).hexdigest()
    for p in day.iterdir():
        assert KEY.encode() not in (gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes())


def test_quota_fault_ends_the_sweep_pauses_and_slows_down(tmp_path):
    clock = Clock(sgt(7, 0))
    stops = [f"{i:05d}" for i in range(10)]
    session = Session(clock, quota_at={4})
    n, calls, failed, quota = ba.poll(tmp_path, sgt(7, 20), KEY, stops, session=session, clock=clock,
                                      sleep=clock.sleep)
    assert quota == 1 and failed == 0
    assert 60 in clock.slept                       # the first pause
    day = tmp_path / "data" / "raw" / "busarrival" / "2026-10-16"
    m = pd.read_csv(day / "manifest.csv")
    assert m.loc[0, ["ok", "quota_faults", "skipped"]].tolist() == [3, 1, 6]
    assert m.period_s.tolist()[:2] == [120, 240]   # doubled after the fault
    first_after = [t for t, _ in session.calls][4]
    assert (first_after - session.calls[3][0]).total_seconds() >= 60


def test_http_429_counts_as_quota():
    clock = Clock(sgt(7, 0))
    s = Session(clock, status_at={1: 429})
    with pytest.raises(ba.QuotaExceeded):
        ba.get_arrivals(s, KEY, "92041", sleep=clock.sleep)


def test_other_errors_are_retried_once_then_recorded():
    clock = Clock(sgt(7, 0))
    s = Session(clock, status_at={1: 503, 2: 503})
    status, code, services = ba.get_arrivals(s, KEY, "92041", sleep=clock.sleep)
    assert status.startswith("failed") and code == 503 and services == [] and len(s.calls) == 2
    s = Session(clock, status_at={1: 503})
    assert ba.get_arrivals(s, KEY, "92041", sleep=clock.sleep)[0] == "ok"


def test_cadence_doubles_caps_and_recovers():
    c = ba.Cadence()
    assert [c.quota() for _ in range(4)] == [60, 120, 240, 240]
    assert c.period == 480
    for _ in range(ba.RECOVER_SWEEPS - 1):
        c.ok()
    assert c.period == 480
    c.ok()
    assert c.period == 240
    for _ in range(2 * ba.RECOVER_SWEEPS):
        c.ok()
    assert c.period == 120 and c.faults == 0


def test_chunk_returns_and_keeps_the_period(tmp_path):
    clock = Clock(sgt(7, 0))
    session = Session(clock)
    ba.poll(tmp_path, sgt(10, 0), KEY, ["1", "2"], chunk_minutes=5, session=session, clock=clock, sleep=clock.sleep)
    assert clock() == sgt(7, 6)    # 3 sweeps (07:00, 07:02, 07:04); slept to 07:06, the next sweep time
    ba.poll(tmp_path, sgt(10, 0), KEY, ["1", "2"], chunk_minutes=5, session=session, clock=clock, sleep=clock.sleep)
    t = [t for t, s in session.calls if s == "1"]
    assert all((b - a).total_seconds() == pytest.approx(120) for a, b in zip(t, t[1:]))


def test_workflow_uses_the_secret_only_in_the_poll_step():
    text = (ROOT / ".github" / "workflows" / "busarrival.yml").read_text(encoding="utf-8")
    assert text.count("secrets.LTA_ACCOUNT_KEY") == 1
    import re
    crons = re.findall(r'cron:\s*"([^"]+)"', text)
    assert crons == ["0 22 * * 0-4", "27 22 * * 0-4", "53 22 * * 0-4", "19 23 * * 0-4"]   # Sun-Thu UTC
    for c in crons:   # 06:00, 06:27, 06:53 and 07:19 SGT on a Monday: every trigger can start the morning
        m, h = map(int, c.split()[:2])
        fired = dt.datetime(2026, 10, 18, h, m, tzinfo=dt.timezone.utc)
        start, until, _ = ba.window(fired)
        assert (start, until) == (max(sgt(6, 30, day=19), fired), sgt(10, 0, day=19))
    assert "group: collect-bus-arrivals" in text and "cancel-in-progress: false" in text
    assert "echo $LTA_KEY" not in text and "set -x" not in text


def test_stop_sample_is_the_three_corridors():
    s = pd.read_csv(ROOT / "data" / "reference" / "busarrival_stops.csv", dtype={"BusStopCode": str})
    assert s.groupby("corridor").size().to_dict() == {"Marine Parade Rd": 19, "Orchard Rd": 9, "Telok Blangah Rd": 13}
    assert s.BusStopCode.is_unique and s.BusStopCode.str.len().eq(5).all()
    assert len(s) * 3600 / ba.PERIOD <= 1250   # the documented call budget
