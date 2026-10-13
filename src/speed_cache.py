"""Day-by-day cache of speed-band snapshots, as dense (snapshot x link) matrices of band midpoint speeds.

The raw archive has one gzip CSV per snapshot for all 143,787 road links. Reading thousands of them for every
analysis is slow, so this keeps one compressed .npz per day for the links any analysis here uses:
expressways (road category 1), major arterials (category 2) and every segment matched to a bus link.

The cache is incremental: a day's file is rebuilt only when the list of snapshot files for that day has changed,
so re-running after the collector adds snapshots only reads the new day(s).

Speeds are band midpoints in km/h (band 8, open-ended, is its minimum + 5); band 0 and missing values are NaN.
Snapshot times are naive local time (UTC+08:00, Singapore), taken from the file names.
"""
import datetime as dt
import gzip
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "datamall"
CACHE = ROOT / "data" / "processed" / "speed_cache"
DB = ROOT / "data" / "processed" / "sgbus.duckdb"


def band_kmh(band, lo, hi):
    """Band midpoint speed: (min + max + 1) / 2, band 8 (no upper bound) min + 5, band 0 / missing NaN."""
    band = np.asarray(band, dtype=float)
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    v = np.where(band >= 8, lo + 5, (lo + hi + 1) / 2)
    return np.where((band >= 1) & np.isfinite(v), v, np.nan)


def snapshot_time(path):
    return dt.datetime.strptime(Path(path).name.split(".")[0], "sb_%Y%m%d_%H%M")


def snapshot_files(directory=None):
    d = Path(directory) if directory is not None else RAW / "speedbands"
    return sorted(d.glob("sb_*.csv.gz"), key=snapshot_time)


def watched_links(db=DB, links_csv=None):
    """LinkIDs kept in the cache: categories 1-2 plus every speed-band segment matched to a bus link."""
    links = pd.read_csv(links_csv or RAW / "speedband_links.csv", usecols=["LinkID", "RoadCategory"])
    keep = set(links.loc[links.RoadCategory.isin([1, 2]), "LinkID"])
    if Path(db).exists():
        import duckdb
        con = duckdb.connect(str(db), read_only=True)
        try:
            if "speedband_match" in {t for (t,) in con.execute("show tables").fetchall()}:
                keep |= set(con.execute("select distinct LinkID from speedband_match").df().LinkID)
        finally:
            con.close()
    return np.array(sorted(keep), dtype=np.int64)


def read_snapshot(path, link_index):
    """Speeds of one snapshot file aligned to link_index (NaN where a link is absent)."""
    with gzip.open(path, "rt") as z:
        d = pd.read_csv(z)
    pos = np.searchsorted(link_index, d.LinkID.to_numpy())
    pos_ok = (pos < len(link_index)) & (link_index[np.minimum(pos, len(link_index) - 1)] == d.LinkID.to_numpy())
    out = np.full(len(link_index), np.nan, dtype=np.float32)
    v = band_kmh(d.SpeedBand, d.MinimumSpeed, d.MaximumSpeed)
    out[pos[pos_ok]] = v[pos_ok]
    return out


def _signature(files, link_index):
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode())
    h.update(link_index.tobytes())
    return h.hexdigest()


def build(directory=None, cache=None, link_index=None, verbose=True):
    """Create or refresh one npz per day; returns the list of day files."""
    cache = Path(cache) if cache is not None else CACHE
    cache.mkdir(parents=True, exist_ok=True)
    link_index = watched_links() if link_index is None else np.asarray(link_index, dtype=np.int64)
    by_day = {}
    for f in snapshot_files(directory):
        by_day.setdefault(snapshot_time(f).date(), []).append(f)
    out = []
    for day, files in sorted(by_day.items()):
        path = cache / f"{day:%Y%m%d}.npz"
        sig = _signature(files, link_index)
        if path.exists():
            with np.load(path) as z:
                if str(z["signature"]) == sig:
                    out.append(path)
                    continue
        kmh = np.vstack([read_snapshot(f, link_index) for f in files])
        times = np.array([snapshot_time(f).strftime("%Y-%m-%dT%H:%M") for f in files])
        tmp = path.with_name(path.stem + ".tmp.npz")
        np.savez_compressed(tmp, times=times, links=link_index, kmh=kmh, signature=np.array(sig))
        tmp.replace(path)
        if verbose:
            print(f"speed cache {day}: {len(files)} snapshots x {len(link_index):,} links", flush=True)
        out.append(path)
    return out


def load(days=None, cache=None, links=None):
    """Concatenate cached days -> (times as datetime64[m], link ids, kmh matrix). Optional link subset."""
    cache = Path(cache) if cache is not None else CACHE
    files = sorted(cache.glob("????????.npz"))
    if days is not None:
        wanted = {f"{d:%Y%m%d}" for d in days}
        files = [f for f in files if f.stem in wanted]
    times, mats, link_index = [], [], None
    for f in files:
        with np.load(f) as z:
            li = z["links"]
            if link_index is None:
                link_index = li
            elif not np.array_equal(li, link_index):
                raise ValueError(f"{f.name} was built for a different link set; rebuild the cache")
            k = z["kmh"]
            if links is not None:
                k = k[:, np.searchsorted(li, links)]
            mats.append(k)
            times.append(z["times"].astype("datetime64[m]"))
    if link_index is None:
        return np.array([], dtype="datetime64[m]"), np.array([], dtype=np.int64), np.empty((0, 0), np.float32)
    if links is not None:
        links = np.asarray(links, dtype=np.int64)
        missing = ~np.isin(links, link_index)
        if missing.any():
            raise KeyError(f"{missing.sum()} requested links are not in the cache")
        link_index = links
    return np.concatenate(times), link_index, np.vstack(mats)


if __name__ == "__main__":
    build()
