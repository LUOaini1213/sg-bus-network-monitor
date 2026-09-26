"""Rebuild everything from data/raw: database, analyses, dashboard. Fetching raw data is separate:
  python src/fetch_public.py            # OSM stops, URA boundaries and land use, SingStat population (no key)
  LTA_KEY=... python src/datamall.py static pv   # DataMall routes, services, stops, passenger volume
  LTA_KEY=... bash src/sample_speed.sh 202609301000   # speed-band snapshots every 15 minutes
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STEPS = ["build_db.py", "corridors.py", "demand.py", "coverage.py", "priority.py", "dashboard.py"]

for step in STEPS:
    print(f"== {step}", flush=True)
    subprocess.run([sys.executable, str(ROOT / "src" / step)], check=True, cwd=ROOT)
