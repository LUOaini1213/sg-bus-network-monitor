"""Short-term speed-band forecasts on a road graph: persistence, historical average, LightGBM and a graph neural net.

Network: every expressway speed-band segment (road category 1) plus every segment matched to a bus link with at
least BUS_BPH scheduled AM-peak buses an hour. Edges join a segment to the segments that start where it ends.

Time: snapshots are put on a STEP_MIN-minute grid (each slot keeps the snapshot closest to the slot time, within
MAX_OFFSET_MIN). The older archive was sampled about every 18-20 minutes and the collector from 13 October every
5, then (from 14 October 00:50, because of DataMall's hourly quota) every 10 minutes, so a 20-minute grid is the
finest step all of it shares.

Task: from the last LAGS slots (one hour) predict each segment's speed 1, 2 and 3 slots ahead (20/40/60 min).
A sample needs all input slots and the target slot present; segments with a missing value (band 0) at the target
are left out of the error.

Split by whole days, in time order: training days, then VAL_DAYS validation days (early stopping and model
choice), then the test days from TEST_FROM onward, which no model sees before evaluation.

Errors are in km/h on band midpoints (bands are 10 km/h wide). Uncertainty: 95% intervals from a moving-block
bootstrap over test anchor slots (blocks of BLOCK slots), the same resampled blocks for every model, so paired
differences against persistence get their own interval.
"""
import argparse
import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import speed_cache

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
LINKS_CSV = ROOT / "data" / "raw" / "datamall" / "speedband_links.csv"
STEP_MIN, MAX_OFFSET_MIN, LAGS, HORIZONS = 20, 9, 3, (1, 2, 3)
BUS_BPH = 60
TEST_FROM = dt.date(2026, 10, 13)
VAL_DAYS = 2
BLOCK = 6
N_BOOT = 1000


# ---------------------------------------------------------------- network
def select_links(db=DB, links_csv=LINKS_CSV, bus_bph=BUS_BPH):
    import duckdb
    links = pd.read_csv(links_csv)
    keep = set(links.loc[links.RoadCategory == 1, "LinkID"])
    con = duckdb.connect(str(db), read_only=True)
    try:
        bus = con.execute("""select distinct m.LinkID from speedband_match m
                             join corridor_links c using (from_stop, to_stop) where c.AM_Peak_bph >= ?""",
                          [bus_bph]).df().LinkID
    finally:
        con.close()
    keep |= set(bus)
    return links[links.LinkID.isin(keep)].sort_values("LinkID").reset_index(drop=True)


def build_graph(links, tol_m=15.0):
    """Directed edges i -> j where segment j starts within tol_m of where segment i ends (not its own reverse)."""
    from pyproj import Transformer
    from scipy.spatial import cKDTree
    tr = Transformer.from_crs(4326, 3414, always_xy=True)
    sx, sy = tr.transform(links.StartLon.to_numpy(), links.StartLat.to_numpy())
    ex, ey = tr.transform(links.EndLon.to_numpy(), links.EndLat.to_numpy())
    start = np.c_[sx, sy]
    end = np.c_[ex, ey]
    tree = cKDTree(start)
    src, dst = [], []
    for i, js in enumerate(tree.query_ball_point(end, tol_m)):
        for j in js:
            if j == i:
                continue
            if np.hypot(*(end[j] - start[i])) < tol_m:  # j runs back to where i began: a U-turn pair
                continue
            src.append(i)
            dst.append(j)
    return np.array(src, dtype=np.int64), np.array(dst, dtype=np.int64)


def neighbour_mean(x, src, dst, n):
    """Mean over up- and downstream neighbours of the last axis (NaN-aware); NaN where a node has none observed."""
    x = np.asarray(x, dtype=float)
    s = np.zeros(x.shape[:-1] + (n,))
    c = np.zeros(x.shape[:-1] + (n,))
    for a, b in ((src, dst), (dst, src)):
        v = x[..., a]
        ok = np.isfinite(v)
        np.add.at(s.T, b, np.where(ok, v, 0.0).T)
        np.add.at(c.T, b, ok.T.astype(float))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(c > 0, s / c, np.nan)


# ---------------------------------------------------------------- time grid and samples
def to_grid(times, kmh, step_min=STEP_MIN, max_offset_min=MAX_OFFSET_MIN):
    """(grid slot times, slot x link matrix with NaN rows for empty slots)."""
    t = pd.DatetimeIndex(times)
    if len(t) == 0:
        return np.array([], dtype="datetime64[m]"), np.empty((0, kmh.shape[1]))
    slot = t.round(f"{step_min}min")
    off = np.abs((t - slot).total_seconds() / 60)
    grid = pd.date_range(slot.min(), slot.max(), freq=f"{step_min}min")
    X = np.full((len(grid), kmh.shape[1]), np.nan, dtype=np.float32)
    best = np.full(len(grid), np.inf)
    pos = grid.get_indexer(slot)
    for i in np.argsort(off, kind="stable"):  # closest snapshot first; later ones never replace it
        if off[i] <= max_offset_min and off[i] < best[pos[i]]:
            X[pos[i]] = kmh[i]
            best[pos[i]] = off[i]
    return grid.to_numpy().astype("datetime64[m]"), X


def anchors(X, lags=LAGS, horizons=HORIZONS):
    """Slots t where slots t-lags+1..t and t+max(h) all hold a snapshot."""
    present = np.isfinite(X).any(axis=1)
    hmax = max(horizons)
    ok = [t for t in range(lags - 1, len(X) - hmax)
          if present[t - lags + 1:t + 1].all() and all(present[t + h] for h in horizons)]
    return np.array(ok, dtype=np.int64)


def split(grid, idx, test_from=TEST_FROM, val_days=VAL_DAYS):
    """Whole-day split of anchor indices by the date of the anchor slot. Samples whose input window or targets
    cross midnight are dropped, so no slot is shared between days in different parts of the split."""
    first = pd.DatetimeIndex(grid[idx - (LAGS - 1)]).date
    last = pd.DatetimeIndex(grid[idx + max(HORIZONS)]).date
    idx = idx[first == last]
    d = pd.DatetimeIndex(grid[idx]).date
    test = idx[d >= test_from]
    before = sorted({x for x in d if x < test_from})
    val_dates = set(before[-val_days:]) if len(before) > val_days else set()
    val = idx[np.isin(d, list(val_dates)) & (d < test_from)]
    train = idx[~np.isin(d, list(val_dates)) & (d < test_from)]
    return train, val, test


def time_features(grid_times):
    t = pd.DatetimeIndex(grid_times)
    frac = (t.hour * 60 + t.minute) / 1440.0
    return np.c_[np.sin(2 * np.pi * frac), np.cos(2 * np.pi * frac), (t.weekday < 5).astype(float)]


# ---------------------------------------------------------------- baselines
def persistence(X, idx, h):
    return X[idx]


def ha_table(X, grid, train_idx, slots_per_day):
    """Historical average from training slots: link x (day type, time-of-day slot), falling back to all days, then
    the link's overall training mean."""
    t = pd.DatetimeIndex(grid)
    tod = ((t.hour * 60 + t.minute) // (1440 // slots_per_day)).to_numpy()
    wk = (t.weekday < 5).astype(int)
    rows = np.unique(np.r_[train_idx, *[train_idx - k for k in range(1, LAGS)]])  # every slot seen in training
    rows = rows[(rows >= 0) & np.isfinite(X[rows]).any(axis=1)]
    n = X.shape[1]
    s = np.zeros((2, slots_per_day, n))
    c = np.zeros((2, slots_per_day, n))
    for r in rows:
        ok = np.isfinite(X[r])
        s[wk[r], tod[r], ok] += X[r, ok]
        c[wk[r], tod[r], ok] += 1
    with np.errstate(invalid="ignore", divide="ignore"):
        by_type = s / c
        by_tod = s.sum(0) / c.sum(0)
        overall = s.sum((0, 1)) / c.sum((0, 1))
    table = np.where(np.isfinite(by_type), by_type, by_tod[None])
    return np.where(np.isfinite(table), table, overall[None, None]), tod, wk


def historical_average(table, tod, wk, idx, h):
    return table[wk[idx + h], tod[idx + h]]


def lgbm_frame(X, nb, tf, idx, link_mean, cat, ha=None, h=None):
    """Long feature table: one row per (anchor, link)."""
    n = X.shape[1]
    cols = {}
    for k in range(LAGS):
        cols[f"lag{k}"] = X[idx - k].ravel()
        cols[f"nb_lag{k}"] = nb[idx - k].ravel()
    cols["link_mean"] = np.tile(link_mean, len(idx))
    cols["road_cat"] = np.tile(cat, len(idx))
    for j, name in enumerate(["tod_sin", "tod_cos", "weekday"]):
        tgt = tf[idx + h, j] if h is not None else tf[idx, j]
        cols[name] = np.repeat(tgt, n)
    if ha is not None:
        cols["ha"] = ha.ravel()
    return pd.DataFrame(cols)


def fit_lgbm(X, nb, tf, train, val, link_mean, cat, ha_fn, h, seed=0):
    import lightgbm as lgb
    ftr = lgbm_frame(X, nb, tf, train, link_mean, cat, ha_fn(train, h), h)
    ytr = (X[train + h] - X[train]).ravel()  # predict the change from the last value
    ok = np.isfinite(ytr) & np.isfinite(ftr.lag0.to_numpy())
    fva = lgbm_frame(X, nb, tf, val, link_mean, cat, ha_fn(val, h), h)
    yva = (X[val + h] - X[val]).ravel()
    okv = np.isfinite(yva) & np.isfinite(fva.lag0.to_numpy())
    model = lgb.LGBMRegressor(objective="l1", n_estimators=2000, learning_rate=0.05, num_leaves=63,
                              min_child_samples=200, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
                              random_state=seed, verbose=-1)
    model.fit(ftr[ok], ytr[ok], eval_set=[(fva[okv], yva[okv])],
              callbacks=[lgb.early_stopping(100, verbose=False)])
    return model


def predict_lgbm(model, X, nb, tf, idx, link_mean, cat, ha_fn, h):
    f = lgbm_frame(X, nb, tf, idx, link_mean, cat, ha_fn(idx, h), h)
    base = X[idx]
    pred = model.predict(f).reshape(base.shape) + np.where(np.isfinite(base), base, link_mean[None])
    return pred


# ---------------------------------------------------------------- metrics
def errors_by_anchor(pred, true):
    """Per anchor slot: (sum |e|, sum e^2, count) over links with a finite target and prediction."""
    ok = np.isfinite(true) & np.isfinite(pred)
    e = np.where(ok, pred - true, 0.0)
    return np.abs(e).sum(1), (e ** 2).sum(1), ok.sum(1)


def mae_rmse(stats):
    a, s, c = stats
    n = c.sum()
    return (a.sum() / n, math.sqrt(s.sum() / n)) if n else (float("nan"), float("nan"))


def block_indices(n, block, n_boot, rng):
    """Moving-block bootstrap: n_boot resamples of anchor positions, built from random blocks of length block."""
    block = max(1, min(block, n))
    starts = rng.integers(0, n - block + 1, size=(n_boot, math.ceil(n / block)))
    return (starts[:, :, None] + np.arange(block)[None, None]).reshape(n_boot, -1)[:, :n]


def boot_ci(stats, idx_sets, base=None):
    """95% intervals of MAE (and of MAE minus the base model's MAE, paired) over the same resamples."""
    a, _, c = stats
    maes = a[idx_sets].sum(1) / c[idx_sets].sum(1)
    out = {"mae_lo": float(np.percentile(maes, 2.5)), "mae_hi": float(np.percentile(maes, 97.5))}
    if base is not None:
        ba, _, bc = base
        d = maes - ba[idx_sets].sum(1) / bc[idx_sets].sum(1)
        out.update(diff_vs_persistence_lo=float(np.percentile(d, 2.5)),
                   diff_vs_persistence_hi=float(np.percentile(d, 97.5)))
    return out


# ---------------------------------------------------------------- run
def run(test_from=TEST_FROM, seeds=(0, 1, 2, 3, 4), gnn=True, out=OUT, epochs=200):
    links = select_links()
    speed_cache.build()
    times, link_ids, kmh = speed_cache.load(links=links.LinkID.to_numpy())
    grid, X = to_grid(times, kmh)
    src, dst = build_graph(links)
    n = X.shape[1]
    idx = anchors(X)
    train, val, test = split(grid, idx, test_from)
    if min(len(train), len(val), len(test)) == 0:
        raise SystemExit(f"not enough data: train {len(train)}, val {len(val)}, test {len(test)} anchor slots")
    slots_per_day = 1440 // STEP_MIN
    table, tod, wk = ha_table(X, grid, train, slots_per_day)
    ha_fn = lambda ix, h: historical_average(table, tod, wk, ix, h)  # noqa: E731
    nb = neighbour_mean(X, src, dst, n)
    tf = time_features(grid)
    train_rows = np.unique(np.r_[train, *[train - k for k in range(1, LAGS)]])
    link_mean = np.nanmean(np.where(np.isfinite(X[train_rows]), X[train_rows], np.nan), axis=0)
    link_mean = np.where(np.isfinite(link_mean), link_mean, np.nanmean(link_mean))
    cat = links.RoadCategory.to_numpy(float)
    am = np.asarray([dt.time(7, 30) <= pd.Timestamp(g).time() <= dt.time(9, 30) and pd.Timestamp(g).weekday() < 5
                     for g in grid[test]])
    rng = np.random.default_rng(2026)
    boot_all = block_indices(len(test), BLOCK, N_BOOT, rng)
    preds = {}
    for h in HORIZONS:
        true = X[test + h]
        preds[("persistence", h)] = persistence(X, test, h)
        preds[("historical_average", h)] = ha_fn(test, h)
        model = fit_lgbm(X, nb, tf, train, val, link_mean, cat, ha_fn, h)
        preds[("lightgbm", h)] = predict_lgbm(model, X, nb, tf, test, link_mean, cat, ha_fn, h)
    gnn_info = None
    if gnn:
        import forecast_gnn
        gnn_preds, gnn_info = forecast_gnn.train_and_predict(X, grid, tf, src, dst, train, val, test, link_mean,
                                                             seeds=seeds, epochs=epochs)
        for h in HORIZONS:
            preds[("gnn_mean_of_seeds", h)] = gnn_preds["mean"][h]
            for s, p in gnn_preds["per_seed"].items():
                preds[(f"gnn_seed{s}", h)] = p[h]
    rows = []
    for (name, h), p in preds.items():
        true = X[test + h]
        st = errors_by_anchor(p, true)
        base = errors_by_anchor(preds[("persistence", h)], true)
        mae, rmse = mae_rmse(st)
        am_mae, am_rmse = mae_rmse(tuple(x[am] for x in st))
        rows.append({"model": name, "horizon_min": h * STEP_MIN, "mae": mae, "rmse": rmse,
                     **boot_ci(st, boot_all, None if name == "persistence" else base),
                     "am_peak_mae": am_mae, "am_peak_rmse": am_rmse, "n_errors": int(st[2].sum())})
    metrics = pd.DataFrame(rows)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    metrics.round(4).to_csv(out / "forecast_metrics.csv", index=False, lineterminator="\n")
    day = lambda ix: sorted({str(x) for x in pd.DatetimeIndex(grid[ix]).date})  # noqa: E731
    summary = {"links": int(n), "edges": int(len(src)), "expressway_links": int((links.RoadCategory == 1).sum()),
               "bus_corridor_min_bph": BUS_BPH, "step_min": STEP_MIN, "lags": LAGS,
               "horizons_min": [h * STEP_MIN for h in HORIZONS],
               "snapshots": int(len(times)), "grid_slots": int(len(grid)),
               "filled_slots": int(np.isfinite(X).any(axis=1).sum()),
               "anchors": {"train": int(len(train)), "val": int(len(val)), "test": int(len(test)),
                           "test_am_peak": int(am.sum())},
               "days": {"train": day(train), "val": day(val), "test": day(test)},
               "bootstrap": {"block_slots": BLOCK, "resamples": N_BOOT}, "gnn": gnn_info}
    (out / "forecast_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return metrics, summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-from", default=str(TEST_FROM))
    ap.add_argument("--no-gnn", action="store_true")
    ap.add_argument("--seeds", type=int, default=5)
    a = ap.parse_args()
    m, s = run(dt.date.fromisoformat(a.test_from), seeds=tuple(range(a.seeds)), gnn=not a.no_gnn)
    print(json.dumps({k: s[k] for k in ["links", "edges", "anchors", "days"]}, indent=1))
    print(m[~m.model.str.startswith("gnn_seed")].round(3).to_string(index=False))
