"""Bus-priority screening over several weekday mornings, and how stable the ranking is from one morning to the next.

Same screening rule as priority.py (see its docstring), applied morning by morning:

1. For every snapshot, a bus link's speed is the length-weighted mean band speed of its matched segments, kept
   only when segments with valid speeds cover at least half the link (MIN_VALID_COVERAGE).
2. A morning is a weekday that is not a public holiday with at least MIN_SNAPSHOTS archive snapshots in
   07:30-09:30. A link's speed on that morning is the median of its covered snapshots, if it has at least
   MIN_SNAPSHOTS of them.
3. Night reference: median of all covered snapshots 22:00-05:00 over the whole archive (at least MIN_SNAPSHOTS).
4. Multi-morning peak speed = median of the link's daily peak speeds; a link needs a daily speed on at least half
   of the mornings (rounded up). Bus-hours lost per hour is then computed exactly as in priority.screen_priority.
5. Stability, for every pair of mornings, over the links eligible on both: overlap of the two top-20 lists and
   Kendall's tau-b of the bus-hours lost. Each morning is also compared with the multi-morning ranking.

The analysis reads the speed cache (speed_cache.py), so it re-runs in seconds as the collector adds mornings.
"""
import datetime as dt
import hashlib
import itertools
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import sparse, stats

import priority
import speed_cache
from demand import HOLIDAYS_2026

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
MIN_SNAPSHOTS = 4
TOP_K = 20
KEY = ["from_stop", "to_stop"]


def in_window(times, window):
    """Boolean mask of datetime64 times inside a (lo, hi) time-of-day window; hi < lo wraps past midnight."""
    lo, hi = window
    t = pd.DatetimeIndex(times)
    mins = t.hour * 60 + t.minute
    a, b = lo.hour * 60 + lo.minute, hi.hour * 60 + hi.minute
    return np.asarray((mins >= a) & (mins <= b) if a < b else (mins >= a) | (mins <= b))


def link_matrix(m, links):
    """Sparse (bus link x speed-band link) matrix of matched segment lengths, plus the bus-link index and lengths."""
    m = m[np.isfinite(m.seg_m) & (m.seg_m > 0) & np.isfinite(m.link_km) & (m.link_km > 0)]
    m = m[m.LinkID.isin(links)]
    keys = m[KEY].drop_duplicates().sort_values(KEY).reset_index(drop=True)
    row = keys.reset_index().merge(m, on=KEY)
    col = np.searchsorted(links, row.LinkID.to_numpy())
    w = sparse.csr_matrix((row.seg_m.to_numpy(float), (row["index"].to_numpy(), col)),
                          shape=(len(keys), len(links)))
    km = row.groupby("index").link_km.first().reindex(range(len(keys))).to_numpy(float)
    return w, pd.MultiIndex.from_frame(keys), km


def snapshot_link_speeds(kmh, w, link_km, chunk=500):
    """(snapshot x bus link) speeds; NaN where valid segments cover less than half the link."""
    out = np.full((len(kmh), w.shape[0]), np.nan)
    for i in range(0, len(kmh), chunk):  # chunks keep memory flat as the archive grows
        k = kmh[i:i + chunk]
        valid = np.isfinite(k) & (k > 0)
        num = np.asarray(w @ np.where(valid, k, 0.0).T).T
        den = np.asarray(w @ valid.T.astype(float)).T
        covered = (den > 0) & (den >= priority.MIN_VALID_COVERAGE * link_km * 1000)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[i:i + chunk] = np.where(covered, num / den, np.nan)
    return out


def period_median(speeds, min_snapshots=MIN_SNAPSHOTS):
    """Per-link median over rows, NaN where fewer than min_snapshots rows are covered."""
    n = np.isfinite(speeds).sum(axis=0)
    return np.where(n >= min_snapshots, nanmedian(speeds), np.nan)


def nanmedian(x):
    """Column medians ignoring NaN; all-NaN (or empty) columns give NaN without a warning."""
    x = np.asarray(x, dtype=float)
    out = np.full(x.shape[1], np.nan)
    ok = np.isfinite(x).any(axis=0)
    if ok.any():
        out[ok] = np.nanmedian(x[:, ok], axis=0)
    return out


def mornings(times, holidays=HOLIDAYS_2026, min_snapshots=MIN_SNAPSHOTS):
    """Weekday, non-holiday dates with at least min_snapshots snapshots in the AM window."""
    am = in_window(times, priority.AM)
    d = pd.DatetimeIndex(times[am]).normalize()
    counts = pd.Series(1, index=d).groupby(level=0).sum()
    return [x.date() for x, n in counts.items()
            if x.weekday() < 5 and x.date() not in holidays and n >= min_snapshots]


def daily_peak(times, speeds, days, min_snapshots=MIN_SNAPSHOTS):
    am = in_window(times, priority.AM)
    dates = pd.DatetimeIndex(times).date
    return {d: period_median(speeds[am & (dates == d)], min_snapshots) for d in days}


def loss(cl, v_peak, v_ref):
    """Bus-hours lost per hour for the corridor rows of cl, via priority.screen_priority."""
    x = cl.assign(v_peak=v_peak, v_ref=v_ref)
    return priority.screen_priority(x)


def topk_overlap(a, b, k=TOP_K):
    """Share of the top-k of a that is also in the top-k of b, over links scored in both (Series indexed by link)."""
    common = a.dropna().index.intersection(b.dropna().index)
    if len(common) < k:
        return float("nan")
    ta = set(a[common].sort_values(ascending=False).index[:k])
    tb = set(b[common].sort_values(ascending=False).index[:k])
    return len(ta & tb) / k


def kendall(a, b):
    common = a.dropna().index.intersection(b.dropna().index)
    if len(common) < 3:
        return float("nan")
    return float(stats.kendalltau(a[common], b[common]).statistic)


def _median_or_none(values):
    v = [x for x in values if np.isfinite(x)]
    return float(np.median(v)) if v else None


def stability(per_day, combined, k=TOP_K):
    """Pairwise and morning-vs-combined agreement. per_day: {date: Series of bus-hours lost, link-indexed}."""
    pairs = [{"a": str(a), "b": str(b), "common_links": int(len(per_day[a].dropna().index.intersection(
                  per_day[b].dropna().index))),
              f"top{k}_overlap": topk_overlap(per_day[a], per_day[b], k), "kendall_tau": kendall(per_day[a], per_day[b])}
             for a, b in itertools.combinations(sorted(per_day), 2)]
    vs = [{"morning": str(d), f"top{k}_overlap_with_combined": topk_overlap(s, combined, k),
           "kendall_tau_with_combined": kendall(s, combined)} for d, s in sorted(per_day.items())]
    in_top = pd.Series(0, index=combined.index, dtype=int)
    for s in per_day.values():
        s = s.dropna()
        in_top[in_top.index.isin(s.sort_values(ascending=False).index[:k])] += 1
    top_combined = combined.dropna().sort_values(ascending=False).index[:k]
    summary = {
        "mornings": len(per_day),
        f"median_pairwise_top{k}_overlap": _median_or_none([p[f"top{k}_overlap"] for p in pairs]),
        "median_pairwise_kendall_tau": _median_or_none([p["kendall_tau"] for p in pairs]),
        f"combined_top{k}_in_daily_top{k}_on_half_or_more_mornings": int(
            (in_top[top_combined] >= math.ceil(len(per_day) / 2)).sum()) if per_day else 0,
    }
    return {"summary": summary, "pairs": pairs, "vs_combined": vs}, in_top


def screen(m, cl, times, kmh, links, holidays=HOLIDAYS_2026, min_snapshots=MIN_SNAPSHOTS):
    """Returns (combined ranking, per-morning long table, stability dict, metadata dict)."""
    w, index, km = link_matrix(m, links)
    sp = snapshot_link_speeds(kmh, w, km)
    days = mornings(times, holidays, min_snapshots)
    night = in_window(times, priority.NIGHT)
    v_ref = pd.Series(period_median(sp[night], min_snapshots), index=index)
    daily = {d: pd.Series(v, index=index) for d, v in daily_peak(times, sp, days, min_snapshots).items()}
    cl = cl.set_index(KEY)
    rows, per_day = [], {}
    for d, v in daily.items():
        r = loss(cl.reset_index(), v.reindex(cl.index).to_numpy(float), v_ref.reindex(cl.index).to_numpy(float))
        r = r.assign(morning=str(d), rank=np.arange(1, len(r) + 1))
        rows.append(r[KEY + ["morning", "v_peak", "v_ref", "bus_h_lost_per_h", "rank"]])
        per_day[d] = r.set_index(KEY).bus_h_lost_per_h
    if daily:
        stack = np.vstack([v.to_numpy() for v in daily.values()])
        need = math.ceil(len(daily) / 2)
        enough = np.isfinite(stack).sum(axis=0) >= need
        med = nanmedian(stack)
        v_peak = pd.Series(np.where(enough, med, np.nan), index=index)
        n_days = pd.Series(np.isfinite(stack).sum(axis=0), index=index)
    else:
        need, v_peak, n_days = 0, pd.Series(np.nan, index=index), pd.Series(0, index=index)
    ranked = loss(cl.reset_index(), v_peak.reindex(cl.index).to_numpy(float), v_ref.reindex(cl.index).to_numpy(float))
    ranked["mornings_observed"] = n_days.reindex(ranked.set_index(KEY).index).fillna(0).astype(int).to_numpy()
    combined = ranked.set_index(KEY).bus_h_lost_per_h
    stab, in_top = stability(per_day, combined)
    ranked[f"mornings_in_daily_top{TOP_K}"] = in_top.reindex(ranked.set_index(KEY).index).fillna(0).astype(int).to_numpy()
    by_day = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=KEY + ["morning", "v_peak", "v_ref", "bus_h_lost_per_h", "rank"])
    am = in_window(times, priority.AM)
    dates = pd.DatetimeIndex(times).date

    def window(mask):
        t = pd.DatetimeIndex(times[mask])
        return {"min_snapshot": t.min().isoformat() if len(t) else None,
                "max_snapshot": t.max().isoformat() if len(t) else None, "n_snapshots": int(mask.sum())}

    peak_mask = am & np.isin(dates, days)
    meta = {"mode": "weekday_am_peak_multi_morning", "snapshot_timezone": "Asia/Singapore",
            "peak": window(peak_mask), "night": window(night),
            "mornings": [{"date": str(d), "n_snapshots": int((am & (dates == d)).sum())} for d in days],
            "min_snapshots": min_snapshots, "min_mornings_per_link": need,
            "min_valid_coverage": priority.MIN_VALID_COVERAGE, "eligible_links": int(len(ranked))}
    return ranked, by_day, stab, meta


def publish(ranked, by_day, stab, meta, output_dir=None):
    out = Path(output_dir) if output_dir is not None else OUT
    out.mkdir(parents=True, exist_ok=True)
    csv_bytes = ranked.to_csv(index=False, lineterminator="\n").encode("utf-8")
    meta = dict(meta, csv_sha256=hashlib.sha256(csv_bytes).hexdigest())
    (out / "priority_screen.csv").write_bytes(csv_bytes)
    (out / "priority_screen_metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    by_day.to_csv(out / "priority_by_morning.csv", index=False, lineterminator="\n")
    (out / "priority_stability.json").write_text(json.dumps(stab, indent=2), encoding="utf-8")
    return meta


def main():
    import duckdb
    con = duckdb.connect(str(priority.DB), read_only=True)
    m = con.execute("select * from speedband_match").df()
    cl = con.execute("""select from_stop, to_stop, services, AM_Peak_bph, link_km,
                               from_Latitude, from_Longitude, to_Latitude, to_Longitude from corridor_links
                        where link_km > 0 and link_km <= ?""", [priority.MAX_LINK_KM]).df()
    con.close()
    m = m.drop(columns=[c for c in ["link_km"] if c in m]).merge(cl[KEY + ["link_km"]], on=KEY)
    cover = m.groupby(KEY).seg_m.sum().rename("matched_m")
    cl = cl.join(cover, on=KEY)
    cl["match_ratio"] = (cl.matched_m / (cl.link_km * 1000)).clip(upper=1)
    speed_cache.build()
    links = np.array(sorted(m.LinkID.unique()), dtype=np.int64)
    times, links, kmh = speed_cache.load(links=links)
    ranked, by_day, stab, meta = screen(m, cl, times, kmh, links)
    meta = publish(ranked, by_day, stab, meta)
    print(f"mornings: {[x['date'] for x in meta['mornings']]}; night snapshots {meta['night']['n_snapshots']}; "
          f"eligible links {meta['eligible_links']:,}; positive loss {(ranked.bus_h_lost_per_h > 0).sum():,}")
    print(json.dumps(stab["summary"], indent=2))


if __name__ == "__main__":
    main()
