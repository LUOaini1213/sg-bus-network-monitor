"""Bus Arrival collector: the next three buses of every service at 41 corridor stops, every 2 minutes.

    python src/busarrival.py gate [--job-start S] [--force --minutes N]   # GitHub Actions: poll this morning? until when?
    python src/busarrival.py poll --out DIR --until T [--start T] [--chunk-minutes 15]   # needs LTA_KEY

Stops: data/reference/busarrival_stops.csv (Marine Parade Rd, Telok Blangah Rd and Orchard Rd; see
src/busarrival_stops.py). Endpoint: DataMall v3/BusArrival?BusStopCode=..., one call per stop.

Call budget. DataMall does not publish its quota. Speed-band pulls failed with "Rate limit quota violation" at
1,440-2,016 calls an hour, made in bursts of four parallel page requests, while a Traffic Incidents poll every
2 minutes kept working through the same hours; 1,152 calls an hour in 15-minute bursts never failed. This
collector therefore never bursts: one sweep is 41 calls spread evenly over the 120-second period (one call every
2.9 s, one at a time), which is 1,230 calls an hour and about 4,300 calls in a 06:30-10:00 morning.
A quota fault (HTTP 429, or any HTTP error whose body mentions the quota) ends the sweep: the stops not yet
polled are recorded as skipped, the collector waits QUOTA_PAUSE seconds (60, then 120, then 240), and the period
doubles, up to MAX_PERIOD (480 s, 308 calls an hour). After RECOVER_SWEEPS clean sweeps in a row the period
halves again, back to the 120 s base. Other HTTP or network errors are retried once after 2 s and then recorded.

Files. Each sweep is one gzip JSON-lines file, <out>/data/raw/busarrival/YYYY-MM-DD/ba_YYYYMMDD_HHMMSS.jsonl.gz,
one line per stop: {"stop", "t" (when the answer arrived, Singapore time), "status", "http", "services" (the
DataMall Services list as returned)}. The day's manifest.csv has one row per sweep with its SHA-256.
The AccountKey is read from LTA_KEY and sent only as the AccountKey header; it is never printed or written.
"""
import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import pathlib
import sys
import time

import requests

from demand import HOLIDAYS_2026

ROOT = pathlib.Path(__file__).resolve().parents[1]
URL = "https://datamall2.mytransport.sg/ltaodataservice/v3/BusArrival"
STOPS_CSV = ROOT / "data" / "reference" / "busarrival_stops.csv"
SGT = dt.timezone(dt.timedelta(hours=8), "SGT")
WINDOW = (dt.time(6, 30), dt.time(10, 0))
EARLY_MIN = 150  # a scheduled run that starts up to 150 minutes before 06:30 (04:00 SGT) waits for 06:30
JOB_TIMEOUT_MIN = 355  # timeout-minutes in busarrival.yml (GitHub-hosted jobs stop at 360)
FINISH_MIN = 5   # the last chunk's commit and push after polling stops
MAX_RUN_MIN = JOB_TIMEOUT_MIN - FINISH_MIN  # polling stops at most 350 minutes after the job started
PERIOD = 120     # seconds per sweep
MAX_PERIOD = 480
QUOTA_PAUSE = (60, 120, 240)
RECOVER_SWEEPS = 5
MANIFEST_COLUMNS = ["sweep_sgt", "file", "period_s", "stops", "ok", "failed", "skipped", "quota_faults",
                    "services_listed", "seconds", "sha256", "bytes"]
EXIT_DONE = 10   # poll: the window is over (the workflow stops looping)


class QuotaExceeded(RuntimeError):
    """DataMall's rate-limit fault."""


def load_stops(path=STOPS_CSV):
    with open(path, newline="", encoding="utf-8") as f:
        return [r["BusStopCode"] for r in csv.DictReader(f)]


def sgt(now):
    return now.astimezone(SGT)


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def window(now, holidays=HOLIDAYS_2026, win=WINDOW, early_min=EARLY_MIN, job_start=None, max_run_min=MAX_RUN_MIN):
    """(start, until, reason) of this morning's polling window for a run starting at `now` (aware), or
    (None, None, reason) when there is none: weekends, public holidays, more than early_min minutes before
    06:30, or after 10:00 Singapore time. Polling stops at most max_run_min minutes after `job_start` (default
    `now`), so a job that started very early ends inside timeout-minutes."""
    t = sgt(now)
    if t.weekday() >= 5:
        return None, None, f"{t:%a %Y-%m-%d} is a weekend day"
    if t.date() in holidays:
        return None, None, f"{t:%a %Y-%m-%d} is a public holiday"
    start = dt.datetime.combine(t.date(), win[0], SGT)
    until = dt.datetime.combine(t.date(), win[1], SGT)
    if t < start - dt.timedelta(minutes=early_min):
        return None, None, f"{t:%H:%M} SGT is too early (the window opens at {win[0]:%H:%M})"
    if t >= until:
        return None, None, f"{t:%H:%M} SGT is after the window closed at {win[1]:%H:%M}"
    cap = sgt(job_start or now) + dt.timedelta(minutes=max_run_min)
    until = min(until, cap)
    return max(start, t), until, f"{t:%a %Y-%m-%d %H:%M} SGT: polling {max(start, t):%H:%M}-{until:%H:%M}"


def gate(now, force=False, minutes=20, job_start=""):
    """(start, reason, step outputs) of the workflow's gate step for a run at `now` (aware); `job_start` is the
    Unix second the job started (empty if unknown). A manual run (force) polls `minutes` from now."""
    began = dt.datetime.fromtimestamp(int(job_start), dt.timezone.utc) if job_start else None
    start, until, reason = window(now, job_start=began)
    if force:
        start, until = sgt(now), sgt(now) + dt.timedelta(minutes=minutes)
        reason = f"manual run: polling {start:%H:%M}-{until:%H:%M} SGT"
    return start, reason, dict(collect=str(start is not None).lower(),
                               start=f"{start:%Y-%m-%dT%H:%M:%S}" if start else "",
                               until=f"{until:%Y-%m-%dT%H:%M:%S}" if until else "",
                               day=f"{sgt(start or now):%Y-%m-%d}")


def get_arrivals(session, key, stop, sleep=time.sleep):
    """(status, http, services) for one stop. Raises QuotaExceeded on DataMall's rate-limit fault."""
    for attempt in range(2):
        try:
            r = session.get(URL, params={"BusStopCode": stop},
                            headers={"AccountKey": key, "accept": "application/json"}, timeout=30)
            if r.status_code == 429 or (r.status_code >= 400 and "quota" in r.text.lower()):
                raise QuotaExceeded(f"HTTP {r.status_code} rate-limit quota at stop {stop}")
            r.raise_for_status()
            return "ok", r.status_code, r.json().get("Services", [])
        except (requests.RequestException, ValueError) as e:
            if attempt == 1:
                code = getattr(getattr(e, "response", None), "status_code", None)
                return f"failed: {type(e).__name__}", code, []
            sleep(2)


class Cadence:
    """The sweep period: doubles after a quota fault (up to max_period), halves after `recover` clean sweeps."""

    def __init__(self, base=PERIOD, max_period=MAX_PERIOD, pauses=QUOTA_PAUSE, recover=RECOVER_SWEEPS):
        self.base, self.max, self.pauses, self.recover = base, max_period, pauses, recover
        self.period, self.faults, self.clean = base, 0, 0

    def quota(self):
        """Register a quota fault; returns the pause in seconds before the next sweep."""
        pause = self.pauses[min(self.faults, len(self.pauses) - 1)]
        self.faults += 1
        self.clean = 0
        self.period = min(self.period * 2, self.max)
        return pause

    def ok(self):
        self.clean += 1
        if self.clean >= self.recover and self.period > self.base:
            self.period = max(self.base, self.period // 2)
            self.clean = 0
        if self.period == self.base:
            self.faults = 0


def write_sweep(lines, when, directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"ba_{when:%Y%m%d_%H%M%S}.jsonl.gz"
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as z:
        for line in lines:
            z.write(json.dumps(line, separators=(",", ":")) + "\n")
    os.replace(tmp, path)
    return path


def append_manifest(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, MANIFEST_COLUMNS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(row)


def sweep(session, key, stops, period, clock, sleep):
    """Poll each stop once, `period / len(stops)` seconds apart. Returns (lines, quota_fault)."""
    lines, gap, t0 = [], period / len(stops), clock()
    for i, stop in enumerate(stops):
        wait = (t0 + dt.timedelta(seconds=i * gap) - clock()).total_seconds()
        if wait > 0:
            sleep(wait)
        try:
            status, code, services = get_arrivals(session, key, stop, sleep)
        except QuotaExceeded as e:
            lines.append({"stop": stop, "t": sgt(clock()).isoformat(timespec="milliseconds"), "status": "quota",
                          "http": 429, "services": []})
            lines += [{"stop": s, "t": None, "status": "skipped", "http": None, "services": []}
                      for s in stops[i + 1:]]
            log(f"{e}; {len(stops) - i - 1} stops skipped this sweep")
            return lines, True
        lines.append({"stop": stop, "t": sgt(clock()).isoformat(timespec="milliseconds"), "status": status,
                      "http": code, "services": services})
    return lines, False


def poll(out, until, key, stops, start=None, chunk_minutes=None, session=None, clock=utc_now, sleep=time.sleep,
         cadence=None):
    """Sweeps from `start` (default now) until `until`, or for chunk_minutes. Returns (sweeps, calls, failed, quota)."""
    session = session or requests.Session()
    cadence = cadence or Cadence()
    if start is not None and clock() < start:
        sleep((start - clock()).total_seconds())
    stop_at = until if chunk_minutes is None else min(until, clock() + dt.timedelta(minutes=chunk_minutes))
    n_sweeps = calls = failed = quota = 0
    while clock() < stop_at:
        began = clock()
        t = sgt(began).replace(tzinfo=None, microsecond=0)
        lines, fault = sweep(session, key, stops, cadence.period, clock, sleep)
        directory = pathlib.Path(out) / "data" / "raw" / "busarrival" / f"{t:%Y-%m-%d}"
        path = write_sweep(lines, t, directory)
        data = path.read_bytes()
        counts = {s: sum(1 for x in lines if x["status"].startswith(s)) for s in ("ok", "failed", "skipped", "quota")}
        append_manifest(directory / "manifest.csv", {
            "sweep_sgt": f"{t:%Y-%m-%dT%H:%M:%S}", "file": path.name, "period_s": cadence.period, "stops": len(stops),
            "ok": counts["ok"], "failed": counts["failed"], "skipped": counts["skipped"], "quota_faults": counts["quota"],
            "services_listed": sum(len(x["services"]) for x in lines),
            "seconds": round((clock() - began).total_seconds(), 1), "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data)})
        n_sweeps += 1
        calls += counts["ok"] + counts["failed"] + counts["quota"]
        failed += counts["failed"]
        quota += counts["quota"]
        if fault:
            pause = cadence.quota()
            annotate(f"Bus Arrival quota fault in the {t:%H:%M:%S} sweep; pausing {pause} s, "
                     f"period now {cadence.period} s", "warning")
            sleep(pause)
            continue
        cadence.ok()
        nxt = began + dt.timedelta(seconds=cadence.period)
        if nxt >= until:
            break
        rest = (nxt - clock()).total_seconds()
        if rest > 0:  # also at the end of a chunk, so the next chunk's first sweep keeps the period
            sleep(rest)
    return n_sweeps, calls, failed, quota


def log(msg):
    print(f"{dt.datetime.now(SGT):%Y-%m-%d %H:%M:%S} SGT {msg}", flush=True)


def annotate(msg, level="error"):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{msg}", flush=True)
    log(msg)


def github_output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def parse_sgt(text):
    t = dt.datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=SGT)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--force", action="store_true", help="manual run: poll now for --minutes")
    g.add_argument("--minutes", type=int, default=20)
    g.add_argument("--job-start", default="", help="when the job started, Unix seconds (for the timeout bound)")
    p = sub.add_parser("poll")
    p.add_argument("--out", required=True)
    p.add_argument("--until", required=True, help="end, YYYY-mm-ddTHH:MM[:SS] Singapore time")
    p.add_argument("--start", help="first sweep, Singapore time (default now)")
    p.add_argument("--chunk-minutes", type=float, help="return after this long (the workflow commits in between)")
    a = ap.parse_args()
    if a.cmd == "gate":
        start, reason, outputs = gate(utc_now(), a.force, a.minutes, a.job_start)
        print(("collect: " if start else "skip: ") + reason, flush=True)
        github_output(**outputs)
    else:
        key = os.environ.get("LTA_KEY")
        if not key:
            annotate("LTA_KEY is empty: add the LTA_ACCOUNT_KEY repository secret")
            sys.exit(2)
        until = parse_sgt(a.until)
        if utc_now() >= until:
            sys.exit(EXIT_DONE)
        stops = load_stops()
        n, calls, failed, quota = poll(a.out, until, key, stops, start=parse_sgt(a.start) if a.start else None,
                                       chunk_minutes=a.chunk_minutes)
        log(f"{n} sweeps, {calls} calls, {failed} failed, {quota} quota faults")
        if calls and failed > calls / 2:
            annotate(f"{failed} of {calls} Bus Arrival calls failed")
            sys.exit(1)
        sys.exit(EXIT_DONE if utc_now() >= until else 0)
