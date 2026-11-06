"""Rebuild the network analyses from data/raw: database, corridors, demand, coverage, OD, anomalies, priority screen,
safety brief, finance workbook and dashboard. Fetching raw data is separate (see "What can be rebuilt" in the README):
  python src/fetch_public.py            # OSM stops, URA boundaries and land use, SingStat population (no key)
  LTA_KEY=... python src/datamall.py static pv   # DataMall routes, services, stops; stop volumes 2026-02/06/07/08
  LTA_KEY=... python src/datamall.py od          # origin-destination volumes 2026-07 and 2026-08
  LTA_KEY=... python src/collect.py run --until YYYYMMDDHHMM   # speed bands every 15 min, incidents every 2 min
  python src/fetch_safety.py            # SingStat / data.gov.sg safety and ridership tables (no key)
  python src/forecast.py                # speed-band forecasts (PyTorch; separate because it trains models)
  curl -o data/raw/Singapore.osm.gz https://download.bbbike.org/osm/bbbike/Singapore/Singapore.osm.gz

Every step's raw inputs are checked before anything runs. When one is missing, nothing runs: the missing files are
listed and the exit status is 2. `python run_all.py --check` only checks.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
STEPS = ["build_db.py", "corridors.py", "demand.py", "coverage.py", "walk_network.py", "coverage_walk.py", "od.py",
         "anomalies.py", "priority.py", "safety.py", "finance_workbook.py", "dashboard.py"]
DM = "datamall/"
# Raw inputs per step, as glob patterns under data/raw. The other steps read the database and committed files only.
INPUTS = {
    "build_db.py": [DM + "BusRoutes_*.json", DM + "BusServices_*.json", DM + "BusStops_*.json",
                    *(DM + f"transport_node_bus_{m}*.csv" for m in ("202602", "202606", "202607", "202608")),
                    "osm_bus_stops.json", "mp2019_planning_area.geojson"],
    "coverage.py": ["singstat_C020123_ghs2025.json", "mp2019_land_use.geojson", "mp2019_subzone.geojson"],
    "walk_network.py": ["Singapore.osm.gz"],
    "od.py": [DM + "origin_destination_bus_202607.csv", DM + "origin_destination_bus_202608.csv"],
    "priority.py": [DM + "speedband_links.csv", DM + "speedbands/**/sb_*.csv.gz"],
    "safety.py": [DM + "speedband_links.csv", DM + "incidents/inc_*.jsonl"],
}


def walk_cached():
    return (ROOT / "data" / "processed" / "walk_edges.parquet").exists()


def missing_inputs(raw=RAW, steps=STEPS, skip_walk=None):
    """{step: [patterns that match no file]} over the steps that would run."""
    skip_walk = walk_cached() if skip_walk is None else skip_walk
    out = {}
    for step in steps:
        if step == "walk_network.py" and skip_walk:
            continue
        gone = [p for p in INPUTS.get(step, []) if next(iter(Path(raw).glob(p)), None) is None]
        if gone:
            out[step] = gone
    return out


def main(argv):
    gone = missing_inputs()
    if gone:
        print("Missing raw inputs under data/raw, so nothing was run:", file=sys.stderr)
        for step, patterns in gone.items():
            print(f"  {step}: {', '.join(patterns)}", file=sys.stderr)
        print("See 'What can be rebuilt' in the README. Some of these cannot be fetched again: DataMall serves the "
              "current network only (the outputs used the 2026-09-26 pull) and passenger volumes for recent months "
              "only, and the laptop speed-band and incident archives are not published.", file=sys.stderr)
        return 2
    if "--check" in argv:
        print("all raw inputs present")
        return 0
    for step in STEPS:
        if step == "walk_network.py" and walk_cached():
            continue  # the OSM parse takes minutes; rebuild only when the extract changes (delete the parquet files)
        print(f"== {step}", flush=True)
        subprocess.run([sys.executable, str(ROOT / "src" / step)], check=True, cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
