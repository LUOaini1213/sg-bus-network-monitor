"""Long-running DataMall collector: traffic speed bands and traffic incidents.

    python src/collect.py run --until 202610302359      # needs LTA_KEY in the environment
    python src/collect.py status                        # pid, last snapshot, last incident poll
    python src/collect.py stop                          # asks the running collector to exit (within ~10 s)
    python src/collect.py gate [--force]                # GitHub Actions: is now a weekday 06:30-10:00 SGT morning?
    python src/collect.py once --out DIR --stamp T      # GitHub Actions: one speed-band pull + one incident poll
    python src/collect.py next --start T --until T --manifest F [--first]   # GitHub Actions loop: wait, print stamp

Speed bands: one snapshot every --speed-every seconds (default 900), written
atomically to data/raw/datamall/speedbands/sb_YYYYMMDD_HHMM.csv.gz in the same format as the older 15-minute
sampler. A snapshot whose row count differs from the expected network size is still kept, and logged.
A full snapshot takes 288 paged calls, and DataMall enforces a call quota whose window is not published. On 13-14
October 2026 pulls failed with "Rate limit quota violation" after 5-7 snapshots in an hour at 5- and 10-minute
cadences (1,440-2,016 calls), while incident polls kept working. 15 minutes (1,152 calls an hour) is the cadence
the earlier sampler ran at without a failure. A quota fault is not retried.
Traffic incidents: one poll every --incident-every seconds (default 120), appended as one JSON line per poll to
data/raw/datamall/incidents/inc_YYYYMMDD.jsonl. DataMall only serves the incidents that are current, so this
archive starts on the day the collector starts.

Timestamps are the machine's local time, which is UTC+08:00 (the same offset as Singapore).
The AccountKey is read from LTA_KEY at start-up and never written anywhere.

GitHub Actions (.github/workflows/collect.yml) runs one job per weekday morning that loops `next` (wait for the next
slot on the 15-minute grid from 06:30, skipping slots another job already pulled) and `once` until the 10:00 pull,
and one job every night that loops the same way on the hour from 22:00 to 05:00, for the night reference (GitHub
dropped the single 01:10 night pull every night of 16-20 October 2026, and on 20 October started the 03:10 one at
05:27, too late). The night trigger fires at 21:07 SGT and two backups follow at 22:37 and 00:13: a job ends by
its timeout, and the backup waiting in the concurrency group takes the night's remaining slots. Pulls after
midnight go in the next day's folder. `gate` (loop_plan) converts the runner's UTC
clock to Singapore time (UTC+08:00, no daylight saving) and skips weekend and public-holiday mornings
(demand.HOLIDAYS_2026); night pulls run every night. GitHub starts scheduled runs up to two hours late, so the
morning job is triggered from 04:10; it waits for 06:30, and a job that started early still ends inside its timeout. `once` makes exactly one speed-band pull; a quota fault
waits QUOTA_BACKOFF seconds and asks for the same page again (three tries in all), and a pull that still fails is
recorded in the day's manifest.csv instead of being dropped silently. A pull that ran out of quota is a warning
and the run exits 0 (the next run is 15 minutes later); any other failed pull fails the run. Files go under <out>/data/raw/speedbands/YYYY-MM-DD/ and
<out>/data/raw/incidents/YYYY-MM-DD/, which the workflow commits to the data-collect branch.
"""
import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from demand import HOLIDAYS_2026

BASE = "https://datamall2.mytransport.sg/ltaodataservice"
RAW = pathlib.Path(__file__).resolve().parents[1] / "data" / "raw" / "datamall"
SB_DIR = RAW / "speedbands"
INC_DIR = RAW / "incidents"
PID_FILE = RAW / "collector.pid"
STOP_FILE = RAW / "collector.stop"
LOG_FILE = RAW / "collector.log"
PAGE = 500
EXPECTED_LINKS = 143787
SGT = dt.timezone(dt.timedelta(hours=8), "SGT")
COLLECT_WINDOW = (dt.time(6, 30), dt.time(10, 0))
NIGHT_WINDOW = (dt.time(22, 0), dt.time(5, 0))  # priority.NIGHT; pulled on the hour, every night
LATE_GRACE_MIN = 20  # GitHub starts scheduled runs late; a 10:00 run that starts at 10:12 still belongs to the morning
QUOTA_BACKOFF = (60, 120)  # waits between the three tries of a page that hit the quota
EARLY_MIN = 150  # a morning loop job may start up to 150 minutes before 06:30 (04:00 SGT) and waits for it
JOB_TIMEOUT_MIN = 355  # timeout-minutes of a morning job in collect.yml (GitHub-hosted jobs stop at 360)
LAST_PULL_MIN = 5  # the last pull and its commit (pulls took 26-72 s on 19 October 2026)
MAX_RUN_MIN = JOB_TIMEOUT_MIN - LAST_PULL_MIN  # a job's last pull starts at most 350 minutes after the job started
NIGHT_CRONS = ("7 13 * * *", "37 14 * * *", "13 16 * * *")  # collect.yml's night triggers, 21:07, 22:37, 00:13 SGT;
# every other cron is a morning one
NIGHT_EARLY_MIN = 60  # a night loop job may start up to 60 minutes before 22:00 (21:00 SGT) and waits for it
LOOP_EVERY = 900  # seconds between the morning loop's pulls (the 15-minute grid from 06:30)
NIGHT_EVERY = 3600  # seconds between the night loop's pulls (on the hour, 22:00-05:00)
MANIFEST_COLUMNS = ["snapshot_sgt", "file", "rows", "sha256", "bytes", "pull_seconds", "quota_retries", "status"]
LINK_COLUMNS = ["LinkID", "RoadName", "RoadCategory", "StartLon", "StartLat", "EndLon", "EndLat"]
ECHO = False  # print log lines (GitHub Actions) instead of appending them to collector.log
_log_lock = threading.Lock()


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with _log_lock:
        if ECHO:  # runners keep UTC; print Singapore time
            print(f"{dt.datetime.now(SGT):%Y-%m-%d %H:%M:%S} SGT {msg}", flush=True)
            return
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class QuotaExceeded(RuntimeError):
    """DataMall answered with its rate-limit fault; retrying only spends more of the quota."""


def get_page(session, key, endpoint, skip, tries=4):
    for i in range(tries):
        try:
            r = session.get(f"{BASE}/{endpoint}", params={"$skip": skip},
                            headers={"AccountKey": key, "accept": "application/json"}, timeout=60)
            if r.status_code >= 400 and "quota" in r.text.lower():
                raise QuotaExceeded(f"rate-limit quota exceeded at $skip={skip}")
            r.raise_for_status()
            return r.json().get("value", [])
        except (requests.RequestException, ValueError):
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def get_page_with_backoff(session, key, endpoint, skip, backoff=(), retries=None):
    """get_page, but after a quota fault wait backoff[i] seconds and ask for the same page again.

    With no backoff (the long-running collector) a quota fault is raised at once. `retries`, if given, collects
    the $skip of every page that hit the quota, for the manifest.
    """
    for wait in backoff:
        try:
            return get_page(session, key, endpoint, skip)
        except QuotaExceeded as e:
            log(f"{endpoint}: {e}; waiting {wait} s before asking again")
            if retries is not None:
                retries.append(skip)
            time.sleep(wait)
    return get_page(session, key, endpoint, skip)


def fetch_all(session, key, endpoint, workers=4, backoff=(), retries=None):
    """All pages of a paged endpoint, fetched `workers` pages at a time until a short page arrives."""
    rows, base = [], 0
    with ThreadPoolExecutor(workers) as ex:
        while True:
            pages = list(ex.map(lambda s: get_page_with_backoff(session, key, endpoint, s, backoff, retries),
                                [base + PAGE * i for i in range(workers)]))
            for p in pages:
                rows += p
                if len(p) < PAGE:
                    return rows
            base += PAGE * workers


def write_snapshot(rows, when, directory=SB_DIR):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"sb_{when:%Y%m%d_%H%M}.csv.gz"
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", newline="") as z:
        w = csv.writer(z)
        w.writerow(["LinkID", "SpeedBand", "MinimumSpeed", "MaximumSpeed"])
        for r in rows:
            w.writerow([r["LinkID"], r["SpeedBand"], r["MinimumSpeed"], r["MaximumSpeed"]])
    os.replace(tmp, path)
    return path


def append_incidents(items, when, directory=INC_DIR):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"inc_{when:%Y%m%d}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"polled_at": when.isoformat(timespec="seconds"), "n": len(items), "value": items}) + "\n")
    return path


def collection_window(now, holidays=HOLIDAYS_2026, window=COLLECT_WINDOW, grace_min=LATE_GRACE_MIN,
                      night=NIGHT_WINDOW):
    """(collect?, reason) for a run starting at `now`: any night (22:00-05:00 SGT, the priority screen's night
    reference), or a weekday that is not a public holiday, 06:30-10:00 SGT. night=None checks the morning only.

    An aware `now` (the runner's UTC clock) is converted to Singapore time; a naive one is taken as Singapore time.
    The end of the morning window is extended by grace_min minutes because GitHub starts scheduled runs late.
    """
    t = sgt_naive(now)
    if night is not None and in_night(t, night):
        return True, f"{t:%a %Y-%m-%d %H:%M} SGT is a night pull (free-flow reference)"
    if t.weekday() >= 5:
        return False, f"{t:%a %Y-%m-%d} is a weekend day"
    if t.date() in holidays:
        return False, f"{t:%a %Y-%m-%d} is a public holiday"
    lo, hi = window
    end = (dt.datetime.combine(t.date(), hi) + dt.timedelta(minutes=grace_min)).time()
    if not lo <= t.time() <= end:
        return False, f"{t:%H:%M} SGT is outside {lo:%H:%M}-{hi:%H:%M} (+{grace_min} min for late starts)"
    if t.year not in {d.year for d in holidays}:
        return True, f"no public holiday list for {t.year} in the repository; collecting anyway"
    return True, f"{t:%a %Y-%m-%d %H:%M} SGT is a weekday morning"


def in_night(t, night=NIGHT_WINDOW):
    """Is naive Singapore time t inside the night window (which wraps past midnight)?"""
    nlo, nhi = night
    return t.time() >= nlo or t.time() <= nhi


def trigger_of(schedule):
    """"night" or "morning" for the cron that started a scheduled run (github.event.schedule); None if unknown."""
    if not schedule:
        return None
    return "night" if schedule.strip() in NIGHT_CRONS else "morning"


def night_of(t, night=NIGHT_WINDOW):
    """(start, end) of the night a naive Singapore time belongs to: 22:00 to 05:00 the next morning. From noon on
    that is the coming night; before noon, the night that ended (or is ending) this morning."""
    lo, hi = night
    d = t.date() if t.time() >= dt.time(12) else t.date() - dt.timedelta(days=1)
    return dt.datetime.combine(d, lo), dt.datetime.combine(d + dt.timedelta(days=1), hi)


def capped(until, began, max_run_min=MAX_RUN_MIN):
    """(until, note): a job's last pull is at most max_run_min minutes after the job began (timeout-minutes)."""
    cap = began + dt.timedelta(minutes=max_run_min)
    if cap < until:
        return cap, f"; last pull by {cap:%H:%M}, {max_run_min} min after the job started"
    return until, ""


def sgt_naive(now):
    return now.astimezone(SGT).replace(tzinfo=None) if now.tzinfo else now


def loop_plan(now, force=False, iterations=1, every=LOOP_EVERY, holidays=HOLIDAYS_2026, early_min=EARLY_MIN,
              trigger=None, job_start=None, max_run_min=MAX_RUN_MIN, night_early_min=NIGHT_EARLY_MIN):
    """(collect?, start, until, reason) for the pull loop of a run starting at `now` (naive Singapore times).

    A weekday morning: one job pulls on the 15-minute grid from 06:30 to 10:00, so it may start up to early_min
    minutes before 06:30 (it waits) or late (it pulls at once, then joins the grid). Every night (the priority
    screen's free-flow reference): one job pulls on the hour from 22:00 to 05:00, so it may start up to
    night_early_min minutes before 22:00, or late. Either way the last pull is at most max_run_min minutes after
    `job_start` (default `now`), so a job ends inside timeout-minutes; a later trigger waiting in the concurrency
    group then takes the slots that are left. A manual run (force) pulls `iterations` times, `every` seconds
    apart, starting now.
    `trigger` is "night" or "morning" (the cron that fired, see trigger_of). None infers it from the time: inside
    the night window is a night run. From 04:00 to 05:00 both are possible, so the workflow passes the cron.
    """
    t = sgt_naive(now).replace(second=0, microsecond=0)
    if force:
        return True, t, t + dt.timedelta(seconds=every * (iterations - 1)), f"manual run: {iterations} pull(s)"
    began = sgt_naive(job_start) if job_start is not None else t
    if trigger is None:
        trigger = "night" if in_night(t) else "morning"
    if trigger == "night":
        start, until = night_of(t)
        if start - dt.timedelta(minutes=night_early_min) <= t <= until:
            until, note = capped(until, began, max_run_min)
            return True, start, until, f"{t:%a %Y-%m-%d %H:%M} SGT: night loop {start:%H:%M}-05:00, hourly{note}"
        nlo, nhi = NIGHT_WINDOW
        return False, None, None, f"{t:%H:%M} SGT: a night trigger outside {nlo:%H:%M}-{nhi:%H:%M} pulls nothing"
    ok, reason = collection_window(t, holidays, night=None)
    lo, hi = COLLECT_WINDOW
    start, until = dt.datetime.combine(t.date(), lo), dt.datetime.combine(t.date(), hi)
    if ok or (t.weekday() < 5 and t.date() not in holidays
              and start - dt.timedelta(minutes=early_min) <= t < start):
        until, note = capped(until, began, max_run_min)
        return True, start, until, f"{t:%a %Y-%m-%d %H:%M} SGT: morning loop {lo:%H:%M}-{hi:%H:%M}{note}"
    return False, None, None, reason


def gate(now, force=False, iterations=1, every=LOOP_EVERY, schedule="", job_start=""):
    """(collect?, reason, step outputs) of the workflow's gate step for a run at `now` (aware, the runner's clock).

    `schedule` is the cron that fired (github.event.schedule, empty for a manual run) and `job_start` the Unix
    second the job started (empty if unknown). `day` and `day2` are the dates of the first and last slot: a night
    loop's pulls after midnight go in the next day's folder, so the workflow checks out and reads both.
    """
    stamp = sgt_minute(now)
    began = dt.datetime.fromtimestamp(int(job_start), dt.timezone.utc) if job_start else None
    ok, start, until, reason = loop_plan(now, force, iterations, every, trigger=trigger_of(schedule), job_start=began)
    night = ok and not force and (trigger_of(schedule) or ("night" if in_night(stamp) else "morning")) == "night"
    first, last = (start, until) if ok else (stamp, stamp)
    return ok, reason, dict(collect=str(ok).lower(), stamp=f"{stamp:%Y-%m-%dT%H:%M}", day=f"{first:%Y-%m-%d}",
                            day2=f"{last:%Y-%m-%d}", start=f"{start:%Y-%m-%dT%H:%M}" if ok else "",
                            until=f"{until:%Y-%m-%dT%H:%M}" if ok else "",
                            every=every if force else (NIGHT_EVERY if night else LOOP_EVERY),
                            grace=0 if night else LATE_GRACE_MIN)


def next_pull(now, start, until, every=LOOP_EVERY, done=(), first=False, grace_min=LATE_GRACE_MIN, force=False):
    """When the loop pulls next (a naive Singapore minute), or None when it should stop.

    The first pull of a job is at once (not before `start`), and may be up to grace_min minutes past `until`
    for a run that GitHub started late, unless another job pulled within the last `every` seconds (a backup
    trigger that ran after the loop). Later pulls are on the grid start + k * every, never past `until`, skipping
    any slot within every / 2 of a pull already in the day's manifest (`done`), so two jobs never pull twice.
    A slot that has passed (a long pull, a quota backoff) is skipped, not caught up.
    """
    step = dt.timedelta(seconds=every)

    def near(x, gap):
        return any(abs(x - d) < gap for d in done)

    if first:
        x = max(now.replace(second=0, microsecond=0), start)
        if x <= until + dt.timedelta(minutes=grace_min) and (force or not near(x, step)):
            return x
    k = math.ceil(max((now - start).total_seconds(), 0) / every)
    x = start + k * step
    while x <= until:
        if not near(x, step / 2):
            return x
        x += step
    return None


def done_pulls(*manifests):
    """Snapshot times of the successful pulls in the days' manifest.csv files (missing files count as empty)."""
    out = []
    for path in dict.fromkeys(pathlib.Path(m) for m in manifests):
        if not path.exists():
            continue
        with open(path, newline="", encoding="utf-8") as f:
            out += [dt.datetime.strptime(r["snapshot_sgt"], "%Y-%m-%dT%H:%M") for r in csv.DictReader(f)
                    if r.get("status", "").startswith("ok")]
    return sorted(out)


def wait_until(t, clock=None, sleep=time.sleep):
    """Sleep until naive Singapore time t."""
    clock = clock or (lambda: dt.datetime.now(SGT).replace(tzinfo=None))
    while (left := (t - clock()).total_seconds()) > 0:
        sleep(min(left, 60))


def sgt_minute(now=None):
    """Naive Singapore time, to the minute (the snapshot file stamp)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(SGT).replace(tzinfo=None, second=0, microsecond=0)


def annotate(msg, level="error"):
    """An error (or warning) annotation on the GitHub Actions run (readable without the job log); a log line
    elsewhere."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}::{msg}", flush=True)
    log(msg)


def github_output(**values):
    """Step outputs for GitHub Actions (ignored outside a workflow)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def write_incident_poll(items, when, directory):
    """One poll as one gzip JSON line, in the format of append_incidents, so files never need rewriting."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"inc_{when:%Y%m%d_%H%M}.jsonl.gz"
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as z:
        z.write(json.dumps({"polled_at": when.isoformat(timespec="seconds"), "n": len(items), "value": items}) + "\n")
    tmp.replace(path)
    return path


def write_links(rows, directory):
    """Speed-band link geometry (speedband_links.csv.gz), rewritten only when it changes. Returns the path or None."""
    if not rows or any(c not in rows[0] for c in LINK_COLUMNS):
        return None
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(LINK_COLUMNS)
    for r in sorted(rows, key=lambda r: int(r["LinkID"])):
        w.writerow([r[c] for c in LINK_COLUMNS])
    data = buf.getvalue().encode("utf-8")
    path = directory / "speedband_links.csv.gz"
    if path.exists() and gzip.decompress(path.read_bytes()) == data:
        return None
    directory.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(data, mtime=0))
    return path


def append_manifest(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, MANIFEST_COLUMNS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(row)


def once(out, stamp, expected_links=EXPECTED_LINKS, backoff=QUOTA_BACKOFF):
    """One speed-band pull and one incident poll, each with a manifest row.

    Returns the number of pulls that failed for a reason other than the quota. A pull still refused for quota
    after the backoff is recorded in the manifest and annotated as a warning, but is not counted: the quota is
    DataMall's to give back, and the next scheduled run tries again.
    """
    global ECHO
    ECHO = True
    key = os.environ.get("LTA_KEY")
    if not key:
        annotate("LTA_KEY is empty: add the LTA_ACCOUNT_KEY repository secret (Settings > Secrets and variables > Actions)")
        raise SystemExit(2)
    out = pathlib.Path(out)
    day = f"{stamp:%Y-%m-%d}"
    session = requests.Session()
    failures, quota, summary = 0, 0, []
    for kind, endpoint, workers in (("speedbands", "v4/TrafficSpeedBands", 4), ("incidents", "TrafficIncidents", 1)):
        directory = out / "data" / "raw" / kind / day
        retries, start = [], time.monotonic()
        row = dict.fromkeys(MANIFEST_COLUMNS, "")
        row.update(snapshot_sgt=f"{stamp:%Y-%m-%dT%H:%M}", rows=0)
        try:
            items = fetch_all(session, key, endpoint, workers=workers, backoff=backoff, retries=retries)
            if kind == "speedbands":
                if not items:
                    raise ValueError("DataMall returned no speed bands")
                path = write_snapshot(items, stamp, directory)
                try:
                    if write_links(items, out / "data" / "raw" / "speedbands" / "links"):
                        log("speed-band link geometry changed; speedband_links.csv.gz rewritten")
                except Exception as e:  # the snapshot is already safe; geometry is refreshed on the next run
                    log(f"link geometry not written: {type(e).__name__}: {str(e)[:200]}")
            else:
                path = write_incident_poll(items, stamp, directory)
            data = path.read_bytes()
            note = f" (expected {expected_links})" if kind == "speedbands" and len(items) != expected_links else ""
            row.update(file=path.name, rows=len(items), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                       status="ok" + note)
        except Exception as e:  # recorded in the manifest and the job log
            row["status"] = f"failed: {type(e).__name__}: {str(e)[:200]}"
            if isinstance(e, QuotaExceeded):  # out of quota after the backoff: a warning, the run exits 0
                quota += 1
                annotate(f"{kind} pull at {stamp:%Y-%m-%d %H:%M} SGT {row['status']}", "warning")
            else:  # anything else fails the job after the commit
                failures += 1
                annotate(f"{kind} pull at {stamp:%Y-%m-%d %H:%M} SGT {row['status']}")
        row.update(pull_seconds=round(time.monotonic() - start, 1), quota_retries=len(retries))
        append_manifest(directory / "manifest.csv", row)
        log(f"{kind} {stamp:%Y-%m-%d %H:%M} SGT: {row['rows']} rows, {row['quota_retries']} quota retries, "
            f"{row['pull_seconds']} s, {row['status']}")
        summary.append(f"{kind} {row['rows']} rows {row['status'].split(':')[0]}")
    github_output(summary="; ".join(summary), failures=failures, quota_failures=quota)
    return failures


def next_tick(now, every):
    """The next multiple of `every` seconds after `now` (local clock)."""
    epoch = now.timestamp()
    return dt.datetime.fromtimestamp((int(epoch // every) + 1) * every)


def _loop(name, every, until, job, stop):
    while not stop.is_set():
        tick = next_tick(dt.datetime.now(), every)
        while not stop.is_set() and dt.datetime.now() < tick:
            stop.wait(min(5.0, max(0.0, (tick - dt.datetime.now()).total_seconds())))
            if STOP_FILE.exists() or dt.datetime.now() >= until:
                stop.set()
        if stop.is_set():
            return
        try:
            job(tick)
        except Exception as e:  # keep collecting after a failed pull; the gap shows in the log and file list
            log(f"{name} failed at {tick:%H:%M}: {type(e).__name__}: {str(e)[:200]}")


def run(until, speed_every, incident_every, expected_links):
    key = os.environ.get("LTA_KEY")
    if not key:
        raise SystemExit("set LTA_KEY in the environment")
    RAW.mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    session = requests.Session()
    stop = threading.Event()

    def speed(tick):
        rows = fetch_all(session, key, "v4/TrafficSpeedBands")
        path = write_snapshot(rows, tick)
        note = "" if len(rows) == expected_links else f" (expected {expected_links})"
        log(f"speed {path.name} {len(rows)} rows{note} in {(dt.datetime.now() - tick).total_seconds():.0f} s")

    def incidents(tick):
        items = fetch_all(session, key, "TrafficIncidents", workers=1)
        append_incidents(items, tick)

    log(f"start pid={os.getpid()} until={until:%Y-%m-%d %H:%M} speed_every={speed_every}s "
        f"incident_every={incident_every}s")
    threads = [threading.Thread(target=_loop, args=("speed", speed_every, until, speed, stop), daemon=True),
               threading.Thread(target=_loop, args=("incidents", incident_every, until, incidents, stop), daemon=True)]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(5)
            if STOP_FILE.exists() or dt.datetime.now() >= until:
                stop.set()
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=120)
        log("exit " + ("on stop file" if STOP_FILE.exists() else "at end time"))
        PID_FILE.unlink(missing_ok=True)
        STOP_FILE.unlink(missing_ok=True)


def status():
    pid = PID_FILE.read_text().strip() if PID_FILE.exists() else None
    snaps = sorted(SB_DIR.glob("sb_*.csv.gz"))
    inc = sorted(INC_DIR.glob("inc_*.jsonl"))
    print(f"pid file: {pid or 'none'}")
    print(f"snapshots: {len(snaps)}, last {snaps[-1].name if snaps else '-'}")
    print(f"incident files: {len(inc)}, last {inc[-1].name if inc else '-'}")
    if LOG_FILE.exists():
        print("log tail:")
        print("".join(LOG_FILE.read_text(encoding="utf-8").splitlines(keepends=True)[-5:]), end="")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--until", required=True, help="end time YYYYmmddHHMM (local)")
    r.add_argument("--speed-every", type=int, default=900)
    r.add_argument("--incident-every", type=int, default=120)
    r.add_argument("--expected-links", type=int, default=EXPECTED_LINKS)
    sub.add_parser("status")
    sub.add_parser("stop")
    g = sub.add_parser("gate", help="plan this run's loop: a weekday morning, a night, or a manual run")
    g.add_argument("--force", action="store_true", help="collect anyway (manual runs)")
    g.add_argument("--iterations", type=int, default=1, help="manual runs: pulls in the loop")
    g.add_argument("--every", type=int, default=LOOP_EVERY, help="manual runs: seconds between pulls")
    g.add_argument("--schedule", default="", help="the cron that started the run (github.event.schedule)")
    g.add_argument("--job-start", default="", help="when the job started, Unix seconds (for the timeout bound)")
    nx = sub.add_parser("next", help="GitHub Actions loop: wait for the next pull and print its stamp")
    nx.add_argument("--start", required=True)
    nx.add_argument("--until", required=True)
    nx.add_argument("--every", type=int, default=LOOP_EVERY)
    nx.add_argument("--manifest", required=True, nargs="+", help="the days' speed-band manifest.csv files")
    nx.add_argument("--grace", type=int, default=LATE_GRACE_MIN, help="minutes a late first pull may pass --until")
    nx.add_argument("--first", action="store_true")
    nx.add_argument("--force", action="store_true", help="the first pull ignores earlier pulls (manual runs)")
    o = sub.add_parser("once", help="one speed-band pull and one incident poll")
    o.add_argument("--out", required=True, help="root of the data-collect checkout")
    o.add_argument("--stamp", help="snapshot time YYYY-mm-ddTHH:MM, Singapore (default: now)")
    o.add_argument("--expected-links", type=int, default=EXPECTED_LINKS)
    a = ap.parse_args()
    if a.cmd == "run":
        run(dt.datetime.strptime(a.until, "%Y%m%d%H%M"), a.speed_every, a.incident_every, a.expected_links)
    elif a.cmd == "gate":
        ok, reason, outputs = gate(dt.datetime.now(dt.timezone.utc), a.force, a.iterations, a.every, a.schedule,
                                   a.job_start)
        print(("collect: " if ok else "skip: ") + reason)
        github_output(**outputs)
    elif a.cmd == "next":
        fmt = "%Y-%m-%dT%H:%M"
        now = dt.datetime.now(SGT).replace(tzinfo=None)
        t = next_pull(now, dt.datetime.strptime(a.start, fmt), dt.datetime.strptime(a.until, fmt), a.every,
                      done_pulls(*a.manifest), a.first, grace_min=a.grace, force=a.force)
        if t is None:
            print("no pull left in this run", file=sys.stderr)
            sys.exit(10)
        wait_until(t)
        print(f"{t:{fmt}}")
    elif a.cmd == "once":
        stamp = dt.datetime.strptime(a.stamp, "%Y-%m-%dT%H:%M") if a.stamp else sgt_minute()
        sys.exit(1 if once(a.out, stamp, a.expected_links) else 0)
    elif a.cmd == "status":
        status()
    else:
        RAW.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text("stop requested " + dt.datetime.now().isoformat(timespec="seconds"))
        print(f"stop file written: {STOP_FILE}; the collector exits within about 10 seconds "
              f"(or after the pull in progress finishes)")
        sys.exit(0)
