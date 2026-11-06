"""Inputs for the priority screen and the forecasts from committed files and the data-collect branch, without the
raw DataMall route files.

    git worktree add ../dc origin/data-collect
    python src/cloud_inputs.py ../dc        # then: python src/priority.py; python src/forecast.py --test-from ...

- corridor_links and stops (the two tables priority.match reads) come from outputs/corridor_links.csv, which
  corridors.py wrote from the September 2026 network. A stop's road name is taken from the links it starts or ends.
- data/raw/datamall/speedband_links.csv (link geometry) comes from the collector's
  data/raw/speedbands/links/speedband_links.csv.gz on data-collect.
- data/raw/datamall/speedbands points at data-collect's day folders (speed_cache reads them recursively).
"""
import gzip
import os
import shutil
import stat
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "datamall"
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
WINDOWS = os.name == "nt"
LINK_COLS = ["from_stop", "to_stop", "services", "AM_Peak_bph", "link_km", "from_Latitude", "from_Longitude",
             "to_Latitude", "to_Longitude"]


def corridor_tables(path=ROOT / "outputs" / "corridor_links.csv"):
    cl = pd.read_csv(path, dtype={"from_stop": str, "to_stop": str})
    stops = pd.concat([cl[["from_stop", "from_RoadName"]].set_axis(["BusStopCode", "RoadName"], axis=1),
                       cl[["to_stop", "to_RoadName"]].set_axis(["BusStopCode", "RoadName"], axis=1)])
    stops = stops.dropna().drop_duplicates("BusStopCode").sort_values("BusStopCode").reset_index(drop=True)
    return cl[LINK_COLS], stops


def is_link(path):
    """A symbolic link, or a Windows directory junction (what link_dir falls back to)."""
    path = Path(path)
    if path.is_symlink():
        return True
    try:
        tag = getattr(os.lstat(path), "st_reparse_tag", 0)
    except OSError:
        return False
    return bool(tag) and tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)


def link_dir(link, target):
    """Point link at the directory target. Windows without the symlink privilege (no Developer Mode, not an
    administrator) refuses symbolic links with WinError 1314, so there a directory junction is made instead; it
    needs no privilege and speed_cache reads through it the same way."""
    try:
        Path(link).symlink_to(target, target_is_directory=True)
    except OSError:
        if not WINDOWS:
            raise
        import _winapi
        _winapi.CreateJunction(str(Path(target).resolve()), str(link))


def main(data_collect):
    dc = Path(data_collect).resolve() / "data" / "raw" / "speedbands"
    if not (dc / "links" / "speedband_links.csv.gz").exists():
        raise SystemExit(f"no link geometry under {dc}; check out the data-collect branch there")
    RAW.mkdir(parents=True, exist_ok=True)
    with gzip.open(dc / "links" / "speedband_links.csv.gz", "rb") as src, open(RAW / "speedband_links.csv", "wb") as dst:
        shutil.copyfileobj(src, dst)
    link = RAW / "speedbands"
    if is_link(link) or link.is_file():
        link.unlink()
    elif link.exists():
        raise SystemExit(f"{link} is a real directory (the laptop archive?); move it before linking data-collect")
    link_dir(link, dc)
    cl, stops = corridor_tables()
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))
    con.execute("create or replace table corridor_links as select * from cl")
    con.execute("create or replace table stops as select * from stops")
    con.close()
    print(f"{len(cl):,} corridor links, {len(stops):,} stops -> {DB}; speedbands -> {dc}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ROOT.parent / "dc")
