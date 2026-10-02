"""Origin-destination analysis from DataMall Passenger Volume by Origin Destination Bus Stops (2026-07, 2026-08).

Each row counts tap-in/tap-out pairs between two stops by hour, summed over all days of a day type in the month;
trips are divided by the day counts in demand.day_counts. Stops are placed in URA MP2019 planning areas.

Outputs:
  od_planning_area_2026-08.csv   weekday trips per day between planning areas
  od_top_pairs_2026-08.csv       busiest stop-to-stop pairs (weekday, per day)
  od_trip_length_2026-08.csv     distribution of straight-line trip lengths
  od_flagged_stops.csv           for each stop flagged by the surveillance, where its August trips go and how that
                                 changed from July (the evidence used in docs/ANOMALIES.md); flag_baseline identifies
                                 the surveillance source of flag/pct_change, not the fixed July-August OD comparison
"""
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd

from demand import day_counts

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
OUT = ROOT / "outputs"
MONTHS = ["2026-07", "2026-08"]


def load(con):
    for ym in MONTHS:
        f = RAW / "datamall" / f"origin_destination_bus_{ym.replace('-', '')}.csv"
        con.execute(f"""create or replace table od_{ym.replace('-', '')} as
            select YEAR_MONTH, DAY_TYPE, cast(TIME_PER_HOUR as int) as "hour",
                   lpad(cast(ORIGIN_PT_CODE as varchar), 5, '0') as o, lpad(cast(DESTINATION_PT_CODE as varchar), 5, '0') as d,
                   TOTAL_TRIPS as trips
            from read_csv_auto('{f.as_posix()}', types={{'ORIGIN_PT_CODE': 'VARCHAR', 'DESTINATION_PT_CODE': 'VARCHAR'}})""")
    stops = con.execute("select BusStopCode, Latitude, Longitude from stops").df()
    pts = gpd.GeoDataFrame(stops, geometry=gpd.points_from_xy(stops.Longitude, stops.Latitude), crs=4326).to_crs(3414)
    pa = gpd.read_file(RAW / "mp2019_planning_area.geojson").to_crs(3414)[["PLN_AREA_N", "geometry"]]
    j = gpd.sjoin(pts, pa, predicate="within", how="left")
    # The only stops outside every MP2019 planning area are the four cross-border stops in Johor Bahru.
    stop_pa = pd.DataFrame({"stop": j.BusStopCode, "pa": j.PLN_AREA_N.fillna("JOHOR BAHRU (cross-border)"),
                            "x": j.geometry.x, "y": j.geometry.y}).drop_duplicates("stop")
    con.execute("create or replace table stop_pa as select * from stop_pa")
    return stop_pa


def weekday_per_day(con, ym):
    n = day_counts(ym)["WEEKDAY"]
    return f"(select o, d, \"hour\", trips / {n}.0 as trips from od_{ym.replace('-', '')} where DAY_TYPE = 'WEEKDAY')"


def quality(con):
    rows = []
    for ym in MONTHS:
        od = con.execute(f"select sum(trips) from od_{ym.replace('-', '')} where DAY_TYPE='WEEKDAY'").fetchone()[0]
        pv = con.execute(f"select sum(TOTAL_TAP_IN_VOLUME) from stop_volume where YEAR_MONTH='{ym}' and DAY_TYPE='WEEKDAY'").fetchone()[0]
        unknown = con.execute(f"""select sum(trips) from od_{ym.replace('-', '')}
                                  where o not in (select stop from stop_pa) or d not in (select stop from stop_pa)""").fetchone()[0] or 0
        rows.append({"month": ym, "od_weekday_trips": od, "stop_volume_weekday_tap_ins": pv,
                     "difference_pct": (od - pv) / pv * 100, "trips_with_unknown_stop": unknown})
    return pd.DataFrame(rows)


def planning_area_matrix(con, ym="2026-08"):
    return con.execute(f"""select po.pa as origin_pa, pd.pa as dest_pa, sum(t.trips) as trips_per_weekday
        from {weekday_per_day(con, ym)} t join stop_pa po on po.stop = t.o join stop_pa pd on pd.stop = t.d
        group by 1, 2 order by 3 desc""").df()


def top_pairs(con, ym="2026-08", n=50):
    return con.execute(f"""select t.o, so.Description as o_name, t.d, sd.Description as d_name, sum(t.trips) as trips_per_weekday,
            sum(case when t."hour" between 7 and 8 then t.trips else 0 end) as am_peak_trips
        from {weekday_per_day(con, ym)} t left join stops so on so.BusStopCode = t.o left join stops sd on sd.BusStopCode = t.d
        group by 1, 2, 3, 4 order by 5 desc limit {n}""").df()


def trip_lengths(con, ym="2026-08"):
    df = con.execute(f"""select sqrt(power(po.x - pd.x, 2) + power(po.y - pd.y, 2)) / 1000 as km, sum(t.trips) as trips
        from {weekday_per_day(con, ym)} t join stop_pa po on po.stop = t.o join stop_pa pd on pd.stop = t.d
        group by 1""").df()
    df = df.sort_values("km")
    cum = df.trips.cumsum() / df.trips.sum()
    bins = [0, 1, 2, 3, 5, 10, 15, 20, 50]
    hist = df.groupby(pd.cut(df.km, bins, right=False), observed=False).trips.sum()
    return hist, float(np.interp(0.5, cum, df.km)), float(np.interp(0.9, cum, df.km))


def flagged_stop_flows(con):
    candidates = []
    for baseline in ("2026-07", "baseline_median"):
        source = pd.read_csv(OUT / f"surveillance_2026-08_vs_{baseline}.csv", dtype={"stop": str})
        source = source.loc[source.flag.isin(["surge", "drop"])].copy()
        source["flag_baseline"] = baseline
        candidates.append(source)
    # Filter before deduplication: a normal July row must not hide a flag from
    # the median baseline. If both flag, retain July's metadata as before.
    s = pd.concat(candidates, ignore_index=True).drop_duplicates("stop")
    flagged = s.stop.tolist()
    con.execute("create or replace temp table flagged as select unnest(?::VARCHAR[]) as stop", [flagged])
    q = lambda ym: f"""select t.o as stop, pd.pa as dest_pa, sum(t.trips) as trips
        from {weekday_per_day(con, ym)} t join stop_pa pd on pd.stop = t.d where t.o in (select stop from flagged) group by 1, 2"""
    jul, aug = con.execute(q("2026-07")).df(), con.execute(q("2026-08")).df()
    m = jul.merge(aug, on=["stop", "dest_pa"], how="outer", suffixes=("_jul", "_aug")).fillna(0)
    m["change"] = m.trips_aug - m.trips_jul
    top = m.sort_values("change", key=abs, ascending=False).groupby("stop").head(3)
    top = top.merge(s[["stop", "Description", "RoadName", "flag", "pct_change", "flag_baseline"]],
                    on="stop", validate="many_to_one")
    return top.sort_values(["flag", "stop", "change"])


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB))
    load(con)
    qc = quality(con)
    qc.to_csv(OUT / "od_quality.csv", index=False)
    print(qc.round(3).to_string(index=False))
    m = planning_area_matrix(con)
    m.to_csv(OUT / "od_planning_area_2026-08.csv", index=False)
    intra = m[m.origin_pa == m.dest_pa].trips_per_weekday.sum() / m.trips_per_weekday.sum()
    print(f"weekday trips/day {m.trips_per_weekday.sum():,.0f}; within the same planning area {intra:.1%}")
    print(m[m.origin_pa != m.dest_pa].head(10).round(0).to_string(index=False))
    tp = top_pairs(con)
    tp.to_csv(OUT / "od_top_pairs_2026-08.csv", index=False)
    print(tp.head(10).round(0).to_string(index=False))
    hist, med, p90 = trip_lengths(con)
    hist.rename("trips_per_weekday").to_csv(OUT / "od_trip_length_2026-08.csv")
    print(f"straight-line trip length: median {med:.2f} km, 90th percentile {p90:.2f} km")
    print((hist / hist.sum()).round(3).to_string())
    ff = flagged_stop_flows(con)
    ff.to_csv(OUT / "od_flagged_stops.csv", index=False)
