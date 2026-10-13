"""Inputs for the bus finance and fare model, derived from DataMall data already in the pipeline.

1. Fare distance of every origin-destination stop pair: the along-route distance (BusRoutes cumulative Distance)
   on the services that call at both stops in that order, by fare class. A pair served by several classes is
   charged the cheapest fare among them (riders can take any of those buses). Pairs with no common service
   (0.4% of rides) get the straight-line distance times the median route/straight-line ratio of the other pairs.
2. Annual rides by fare class and distance band: OD rides per day for each day type in July and August 2026
   (divided by the month's day counts), averaged over the two months, times the 2026 day counts.
3. Scheduled weekday bus-km: for each service and direction, route length x scheduled trips, from the headway
   bands in BusServices and the first/last bus times at the first stop (periods as DataMall defines them).

All three are written to outputs/finance_*.csv and read by finance_model.py, which builds the workbook.
"""
import calendar
import datetime as dt
import math
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from corridors import buses_per_hour
from demand import HOLIDAYS_2026, day_counts

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
OUT = ROOT / "outputs"
MONTHS = ["2026-07", "2026-08"]
N_BANDS = 39          # "Up to 3.2 km", 3.3-4.2, ..., 39.3-40.2, "Over 40.2 km"
# DataMall period definitions for the headway bands (minutes after midnight).
PERIODS = {"AM_Peak_Freq": (390, 510), "AM_Offpeak_Freq": (510, 1020), "PM_Peak_Freq": (1020, 1140),
           "PM_Offpeak_Freq": (1140, 1440 + 180)}


def band_of(km):
    """Fare band index: 0 for up to 3.2 km, then one band per further km (3.3-4.2 km is 1), capped at 38."""
    km = np.round(np.asarray(km, dtype=float), 1)
    idx = np.ceil(np.round(km - 3.2, 1) - 1e-9)
    return np.clip(idx, 0, N_BANDS - 1).astype(int)


def band_label(i):
    if i == 0:
        return "Up to 3.2 km"
    if i == N_BANDS - 1:
        return "Over 40.2 km"
    return f"{2.3 + i:.1f} km - {3.2 + i:.1f} km"


def pair_distances(con):
    return con.execute("""
        with s as (select distinct ServiceNo, Category from bus_services),
        r as (select r.ServiceNo, r.Direction, r.StopSequence seq, r.BusStopCode stop, r.Distance km,
                     case when s.Category in ('FEEDER', 'EXPRESS') then lower(s.Category) else 'trunk' end cls
              from bus_routes r join s using (ServiceNo)),
        p as (select a.stop o, b.stop d, b.km - a.km dist, a.cls
              from r a join r b on a.ServiceNo = b.ServiceNo and a.Direction = b.Direction and b.seq > a.seq
              where b.km - a.km > 0)
        select o, d, min(dist) filter (where cls = 'trunk') trunk_km, min(dist) filter (where cls = 'feeder') feeder_km,
               min(dist) filter (where cls = 'express') express_km
        from p group by 1, 2""").df()


def cheapest_class(trunk_km, feeder_km, express_km, adult_trunk, adult_feeder, adult_express):
    """Fare class and distance a rider pays on the cheapest service serving the pair (adult card fares decide)."""
    cand = []
    for cls, km, table in (("trunk", trunk_km, adult_trunk), ("feeder", feeder_km, None),
                           ("express", express_km, adult_express)):
        if km is None or not np.isfinite(km):
            continue
        fare = adult_feeder if cls == "feeder" else table[band_of(km)]
        cand.append((fare, ("trunk", "feeder", "express").index(cls), cls, km))
    if not cand:
        return None, np.nan
    _, _, cls, km = min(cand)
    return cls, km


def annual_day_counts(year=2026):
    days = [dt.date(year, 1, 1) + dt.timedelta(n) for n in range(366 if calendar.isleap(year) else 365)]
    wd = sum(1 for d in days if d.weekday() < 5 and d not in HOLIDAYS_2026)
    return {"WEEKDAY": wd, "WEEKENDS/HOLIDAY": len(days) - wd}


def rides_by_band(con, fares, months=MONTHS):
    """Annual rides by fare class and band, plus the pair-level table used for distance statistics."""
    pairs = pair_distances(con)
    a = fares["adult"]
    cls_km = [cheapest_class(t, f, e, a["trunk"], a["feeder"], a["express"])
              for t, f, e in zip(pairs.trunk_km, pairs.feeder_km, pairs.express_km)]
    pairs["cls"] = [c for c, _ in cls_km]
    pairs["fare_km"] = [k for _, k in cls_km]
    frames = []
    for ym in months:
        n = day_counts(ym)
        od = con.execute(f"""select o, d, DAY_TYPE day_type, sum(trips) trips from od_{ym.replace('-', '')}
                             group by all""").df()
        od["per_day"] = od.trips / od.day_type.map(n)
        frames.append(od.assign(month=ym))
    od = pd.concat(frames, ignore_index=True)
    od = od.merge(pairs[["o", "d", "cls", "fare_km"]], on=["o", "d"], how="left")
    stops = con.execute("select BusStopCode stop, Latitude lat, Longitude lon from stops").df().set_index("stop")
    known = od.cls.notna()
    # Straight-line fallback for pairs without a common service.
    lat0, lon0 = stops.lat.reindex(od.o).to_numpy(), stops.lon.reindex(od.o).to_numpy()
    lat1, lon1 = stops.lat.reindex(od.d).to_numpy(), stops.lon.reindex(od.d).to_numpy()
    straight = 6371.0 * 2 * np.arcsin(np.sqrt(np.sin(np.radians(lat1 - lat0) / 2) ** 2 + np.cos(np.radians(lat0))
                                              * np.cos(np.radians(lat1)) * np.sin(np.radians(lon1 - lon0) / 2) ** 2))
    ok = known.to_numpy() & np.isfinite(straight) & (straight > 0.05)
    ratio = float(np.median(od.fare_km.to_numpy()[ok] / straight[ok]))
    od.loc[~known, "fare_km"] = straight[~known.to_numpy()] * ratio
    od.loc[~known, "cls"] = "trunk"
    od["fallback"] = ~known
    od["band"] = np.where(od.cls == "feeder", 0, band_of(od.fare_km.fillna(0)))
    year = annual_day_counts()
    od["annual"] = od.per_day * od.day_type.map(year) / len(months)
    table = od.groupby(["cls", "band"]).annual.sum().unstack(0).reindex(range(N_BANDS)).fillna(0.0)
    table = table.reindex(columns=["trunk", "feeder", "express"]).fillna(0.0)
    tr = od[od.cls == "trunk"]
    rep = (tr.fare_km * tr.annual).groupby(tr.band).sum() / tr.annual.groupby(tr.band).sum()
    mid = pd.Series([1.6] + [2.7 + i for i in range(1, N_BANDS)], index=range(N_BANDS))
    table["rep_km"] = rep.reindex(range(N_BANDS)).fillna(mid)
    stats = {"pairs_with_common_service_share_of_rides": float(od.annual[known].sum() / od.annual.sum()),
             "route_to_straight_ratio_median": ratio,
             "annual_rides": float(od.annual.sum()),
             "rides_per_day_by_type": {k: float(od[od.day_type == k].per_day.sum() / len(months)) for k in year},
             "day_counts_2026": year,
             "fare_km_quantiles": {str(q): float(np.interp(q, *_weighted_cdf(od.fare_km, od.annual)))
                                   for q in (0.5, 0.9)}}
    return table, od, stats


def _weighted_cdf(x, w):
    o = np.argsort(x.to_numpy())
    xs = x.to_numpy()[o]
    cw = np.cumsum(w.to_numpy()[o])
    return cw / cw[-1], xs


def hhmm(s):
    s = str(s)
    if not s.isdigit() or len(s) != 4:
        return None
    m = int(s[:2]) * 60 + int(s[2:])
    return m


def scheduled_trips(first, last, bands):
    """Trips a day from first/last bus (minutes after midnight) and the four headway bands. Hours before 06:30
    use the off-peak band (an approximation; DataMall gives no band for them)."""
    if last <= first:
        last += 1440  # last bus after midnight
    windows = dict(PERIODS)
    windows["AM_Offpeak_Freq_early"] = (0, 390)
    trips = 0.0
    for period, (a, b) in windows.items():
        bph = buses_per_hour(bands[period.replace("_early", "")])
        lo, hi = max(a, first), min(b, last)
        if hi > lo:
            trips += bph * (hi - lo) / 60
    return trips


def weekday_bus_km(con):
    """Scheduled weekday bus-km per service and direction from headway bands and first/last bus times."""
    svc = con.execute("select * from bus_services").df()
    routes = con.execute("""select ServiceNo, Direction, max(Distance) route_km,
                                   arg_min(WD_FirstBus, StopSequence) first_bus, arg_min(WD_LastBus, StopSequence) last_bus
                            from bus_routes group by 1, 2""").df()
    s = svc.merge(routes, on=["ServiceNo", "Direction"], how="inner")
    rows = []
    for r in s.itertuples():
        first, last = hhmm(r.first_bus), hhmm(r.last_bus)
        if first is None or last is None:
            rows.append((r.ServiceNo, r.Direction, r.Category, r.route_km, np.nan, np.nan))
            continue
        trips = scheduled_trips(first, last, {p: getattr(r, p) for p in PERIODS})
        rows.append((r.ServiceNo, r.Direction, r.Category, r.route_km, trips, trips * r.route_km))
    return pd.DataFrame(rows, columns=["ServiceNo", "Direction", "Category", "route_km", "weekday_trips",
                                       "weekday_bus_km"])


def build(fares):
    con = duckdb.connect(str(DB), read_only=True)
    try:
        table, od, stats = rides_by_band(con, fares)
        km = weekday_bus_km(con)
    finally:
        con.close()
    OUT.mkdir(exist_ok=True)
    t = table.copy()
    t.insert(0, "band", [band_label(i) for i in range(N_BANDS)])
    t.to_csv(OUT / "finance_rides_by_band.csv", index_label="band_index", lineterminator="\n")
    km.to_csv(OUT / "finance_weekday_bus_km.csv", index=False, lineterminator="\n")
    stats["weekday_bus_km"] = float(km.weekday_bus_km.sum())
    stats["services_with_bus_km"] = int(km.weekday_bus_km.notna().sum())
    return table, od, km, stats


if __name__ == "__main__":
    import json
    import finance_model
    table, od, km, stats = build(finance_model.load_fares())
    print(json.dumps(stats, indent=1))
