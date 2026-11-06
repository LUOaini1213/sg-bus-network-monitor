"""LTA DataMall client. The AccountKey is read from the LTA_KEY environment variable only; never stored."""
import os, io, json, time, zipfile, pathlib, datetime as dt, requests

BASE = "https://datamall2.mytransport.sg/ltaodataservice"
RAW = pathlib.Path(__file__).resolve().parents[1] / "data/raw/datamall"
RAW.mkdir(parents=True, exist_ok=True)

def _hdr():
    return {"AccountKey": os.environ["LTA_KEY"], "accept": "application/json"}

def paged(endpoint):
    out, skip = [], 0
    while True:
        r = requests.get(f"{BASE}/{endpoint}", params={"$skip": skip}, headers=_hdr(), timeout=60)
        r.raise_for_status()
        v = r.json().get("value", [])
        out += v
        if len(v) < 500:
            return out
        skip += 500
        time.sleep(0.2)

def pv_zip(kind, yyyymm):
    """kind: 'Bus' (passenger volume by stop) or 'ODBus' (origin-destination by stop). Returns saved CSV path or None."""
    r = requests.get(f"{BASE}/PV/{kind}", params={"Date": yyyymm}, headers=_hdr(), timeout=60)
    if not r.ok:
        return None
    link = (r.json().get("value") or [{}])[0].get("Link")
    if not link:
        return None
    z = zipfile.ZipFile(io.BytesIO(requests.get(link, timeout=600).content))
    name = z.namelist()[0]
    path = RAW / name
    path.write_bytes(z.read(name))
    return path

def speed_bands_snapshot():
    """Save a compact gzip CSV (LinkID, SpeedBand, Min, Max). Link geometry is stored once in speedband_links.csv."""
    import gzip, csv
    rows = paged("v4/TrafficSpeedBands")
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M")
    d = RAW / "speedbands"; d.mkdir(exist_ok=True)
    with gzip.open(d / f"sb_{ts}.csv.gz", "wt", newline="") as z:
        w = csv.writer(z); w.writerow(["LinkID", "SpeedBand", "MinimumSpeed", "MaximumSpeed"])
        for r in rows:
            w.writerow([r["LinkID"], r["SpeedBand"], r["MinimumSpeed"], r["MaximumSpeed"]])
    return len(rows)

# The months the committed analyses use: demand.py compares August 2026 with February, June and July (stop volumes);
# od.py compares July and August (origin-destination volumes).
PV_MONTHS = ["202602", "202606", "202607", "202608"]
OD_MONTHS = ["202607", "202608"]
KINDS = ("static", "pv", "od", "speed")


def parse_args(argv):
    """`static pv od speed` in any combination, then optional YYYYMM months for pv and od (default: the months
    the analyses use). Returns (kinds, months or None)."""
    kinds = [a for a in argv if a in KINDS]
    months = [a for a in argv if a not in KINDS]
    bad = [m for m in months if not (len(m) == 6 and m.isdigit())]
    if bad:
        raise SystemExit(f"unknown arguments {bad}: expected {' '.join(KINDS)} and YYYYMM months")
    return kinds or ["static", "pv", "speed"], months or None


if __name__ == "__main__":
    import sys
    what, months = parse_args(sys.argv[1:])
    missing = []
    if "static" in what:
        # Saved under the pull date. anomalies.py compares the newest BusRoutes_*.json with the April 2026 pull
        # (or the file given with --routes); the committed outputs used the pull of 2026-09-26.
        for ep in ["BusStops", "BusServices", "BusRoutes"]:
            rows = paged(ep)
            (RAW / f"{ep}_{dt.date.today():%Y%m%d}.json").write_text(json.dumps(rows), encoding="utf-8")
            print(ep, len(rows))
    for kind, api, default in (("pv", "Bus", PV_MONTHS), ("od", "ODBus", OD_MONTHS)):
        if kind in what:
            for m in months or default:
                path = pv_zip(api, m)
                print(f"PV/{api}", m, path or "not served")
                if path is None:
                    missing.append(f"PV/{api} {m}")
    if "speed" in what:
        print("speed bands", speed_bands_snapshot())
    if missing:
        # DataMall serves passenger volumes for recent months only. An older month has to come from an archive.
        raise SystemExit("DataMall did not return: " + ", ".join(missing) + ". DataMall serves passenger volumes "
                         "for recent months only; see 'What can be rebuilt' in the README.")
