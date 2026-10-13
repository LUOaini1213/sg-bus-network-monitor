"""Long-running DataMall collector: traffic speed bands and traffic incidents.

    python src/collect.py run --until 202610302359      # needs LTA_KEY in the environment
    python src/collect.py status                        # pid, last snapshot, last incident poll
    python src/collect.py stop                          # asks the running collector to exit (within ~10 s)

Speed bands: one snapshot every --speed-every seconds (default 300, the rate DataMall refreshes them), written
atomically to data/raw/datamall/speedbands/sb_YYYYMMDD_HHMM.csv.gz in the same format as the older 15-minute
sampler. A snapshot whose row count differs from the expected network size is still kept, and logged.
Traffic incidents: one poll every --incident-every seconds (default 120), appended as one JSON line per poll to
data/raw/datamall/incidents/inc_YYYYMMDD.jsonl. DataMall only serves the incidents that are current, so this
archive starts on the day the collector starts.

Timestamps are the machine's local time, which is UTC+08:00 (the same offset as Singapore).
The AccountKey is read from LTA_KEY at start-up and never written anywhere.
"""
import argparse
import csv
import datetime as dt
import gzip
import json
import os
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://datamall2.mytransport.sg/ltaodataservice"
RAW = pathlib.Path(__file__).resolve().parents[1] / "data" / "raw" / "datamall"
SB_DIR = RAW / "speedbands"
INC_DIR = RAW / "incidents"
PID_FILE = RAW / "collector.pid"
STOP_FILE = RAW / "collector.stop"
LOG_FILE = RAW / "collector.log"
PAGE = 500
_log_lock = threading.Lock()


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with _log_lock, open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_page(session, key, endpoint, skip, tries=4):
    for i in range(tries):
        try:
            r = session.get(f"{BASE}/{endpoint}", params={"$skip": skip},
                            headers={"AccountKey": key, "accept": "application/json"}, timeout=60)
            r.raise_for_status()
            return r.json().get("value", [])
        except (requests.RequestException, ValueError):
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def fetch_all(session, key, endpoint, workers=4):
    """All pages of a paged endpoint, fetched `workers` pages at a time until a short page arrives."""
    rows, base = [], 0
    with ThreadPoolExecutor(workers) as ex:
        while True:
            pages = list(ex.map(lambda s: get_page(session, key, endpoint, s),
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
    r.add_argument("--speed-every", type=int, default=300)
    r.add_argument("--incident-every", type=int, default=120)
    r.add_argument("--expected-links", type=int, default=143787)
    sub.add_parser("status")
    sub.add_parser("stop")
    a = ap.parse_args()
    if a.cmd == "run":
        run(dt.datetime.strptime(a.until, "%Y%m%d%H%M"), a.speed_every, a.incident_every, a.expected_links)
    elif a.cmd == "status":
        status()
    else:
        RAW.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text("stop requested " + dt.datetime.now().isoformat(timespec="seconds"))
        print(f"stop file written: {STOP_FILE}; the collector exits within about 10 seconds "
              f"(or after the pull in progress finishes)")
        sys.exit(0)
