"""Long-running DataMall collector: traffic speed bands and traffic incidents.

    python src/collect.py run --until 202610302359      # needs LTA_KEY in the environment
    python src/collect.py status                        # pid, last snapshot, last incident poll
    python src/collect.py stop                          # asks the running collector to exit (within ~10 s)
    python src/collect.py gate [--force]                # GitHub Actions: is now a weekday 06:30-10:00 SGT morning?
    python src/collect.py once --out DIR --stamp T      # GitHub Actions: one speed-band pull + one incident poll

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

GitHub Actions (.github/workflows/collect.yml) runs `gate` and then `once` every 15 minutes, 06:30-10:00 Singapore
time on weekdays. `gate` converts the runner's UTC clock to Singapore time (UTC+08:00, no daylight saving) and skips
weekends and the public holidays in demand.HOLIDAYS_2026. `once` makes exactly one speed-band pull; a quota fault
waits QUOTA_BACKOFF seconds and asks for the same page again, and a pull that still fails is recorded in the
day's manifest.csv instead of being dropped silently. Files go under <out>/data/raw/speedbands/YYYY-MM-DD/ and
<out>/data/raw/incidents/YYYY-MM-DD/, which the workflow commits to the data-collect branch.
"""
import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
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
LATE_GRACE_MIN = 20  # GitHub starts scheduled runs late; a 10:00 run that starts at 10:12 still belongs to the morning
QUOTA_BACKOFF = (60, 120, 240)
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


def collection_window(now, holidays=HOLIDAYS_2026, window=COLLECT_WINDOW, grace_min=LATE_GRACE_MIN):
    """(collect?, reason) for a run starting at `now`: a weekday that is not a public holiday, 06:30-10:00 SGT.

    An aware `now` (the runner's UTC clock) is converted to Singapore time; a naive one is taken as Singapore time.
    The end of the window is extended by grace_min minutes because GitHub starts scheduled runs late.
    """
    t = now.astimezone(SGT).replace(tzinfo=None) if now.tzinfo else now
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


def sgt_minute(now=None):
    """Naive Singapore time, to the minute (the snapshot file stamp)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.astimezone(SGT).replace(tzinfo=None, second=0, microsecond=0)


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
    """One speed-band pull and one incident poll, each with a manifest row. Returns the number of failed pulls."""
    global ECHO
    ECHO = True
    key = os.environ.get("LTA_KEY")
    if not key:
        raise SystemExit("set LTA_KEY in the environment (the workflow maps the LTA_ACCOUNT_KEY secret to it)")
    out = pathlib.Path(out)
    day = f"{stamp:%Y-%m-%d}"
    session = requests.Session()
    failures, summary = 0, []
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
        except Exception as e:  # recorded in the manifest and the job log; the job fails after the commit
            failures += 1
            row["status"] = f"failed: {type(e).__name__}: {str(e)[:200]}"
        row.update(pull_seconds=round(time.monotonic() - start, 1), quota_retries=len(retries))
        append_manifest(directory / "manifest.csv", row)
        log(f"{kind} {stamp:%Y-%m-%d %H:%M} SGT: {row['rows']} rows, {row['quota_retries']} quota retries, "
            f"{row['pull_seconds']} s, {row['status']}")
        summary.append(f"{kind} {row['rows']} rows {row['status'].split(':')[0]}")
    github_output(summary="; ".join(summary), failures=failures)
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
    g = sub.add_parser("gate", help="is now a weekday 06:30-10:00 SGT morning that is not a public holiday?")
    g.add_argument("--force", action="store_true", help="collect anyway (manual runs)")
    o = sub.add_parser("once", help="one speed-band pull and one incident poll")
    o.add_argument("--out", required=True, help="root of the data-collect checkout")
    o.add_argument("--stamp", help="snapshot time YYYY-mm-ddTHH:MM, Singapore (default: now)")
    o.add_argument("--expected-links", type=int, default=EXPECTED_LINKS)
    a = ap.parse_args()
    if a.cmd == "run":
        run(dt.datetime.strptime(a.until, "%Y%m%d%H%M"), a.speed_every, a.incident_every, a.expected_links)
    elif a.cmd == "gate":
        stamp = sgt_minute()
        ok, reason = collection_window(dt.datetime.now(dt.timezone.utc))
        print(("collect: " if ok else "skip: ") + reason + (" (forced)" if a.force and not ok else ""))
        github_output(collect=str(ok or a.force).lower(), stamp=f"{stamp:%Y-%m-%dT%H:%M}", day=f"{stamp:%Y-%m-%d}")
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
