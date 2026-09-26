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

if __name__ == "__main__":
    import sys
    what = sys.argv[1:] or ["static", "pv", "speed"]
    if "static" in what:
        for ep in ["BusStops", "BusServices", "BusRoutes"]:
            rows = paged(ep)
            (RAW / f"{ep}_{dt.date.today():%Y%m%d}.json").write_text(json.dumps(rows), encoding="utf-8")
            print(ep, len(rows))
    if "pv" in what:
        for m in ["202609", "202608", "202607", "202606", "202605"]:
            print("PV/Bus", m, pv_zip("Bus", m))
    if "od" in what:
        for m in sys.argv[2:] or ["202608"]:
            print("PV/ODBus", m, pv_zip("ODBus", m))
    if "speed" in what:
        print("speed bands", speed_bands_snapshot())
