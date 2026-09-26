"""Corridor sharing: how many services and scheduled buses use each directed stop-to-stop link.

A link is a pair of consecutive stops on a service route (bus_links). Scheduled buses per hour come from the
BusServices headway bands (e.g. AM_Peak_Freq "09-12" = one bus every 9 to 12 minutes, so 60 / 10.5 buses per hour).
Loop services and services without a published headway in a period contribute 0 buses for that period but still
count as a service on the link.
"""
import re
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
OUT = ROOT / "outputs"
PERIODS = ["AM_Peak_Freq", "AM_Offpeak_Freq", "PM_Peak_Freq", "PM_Offpeak_Freq"]


def buses_per_hour(band):
    """'09-12' -> 60 / 10.5; '10' -> 6.0; '-', '' or None -> 0."""
    nums = [int(n) for n in re.findall(r"\d+", str(band or ""))]
    nums = [n for n in nums if n > 0]
    if not nums:
        return 0.0
    return 60.0 / (sum(nums[:2]) / len(nums[:2]))


def build(con):
    links = con.execute("select * from bus_links").df()
    svc = con.execute("select * from bus_services").df()
    for p in PERIODS:
        svc[p.replace("_Freq", "_bph")] = svc[p].map(buses_per_hour)
    bph_cols = [p.replace("_Freq", "_bph") for p in PERIODS]
    links = links.merge(svc[["ServiceNo", "Direction", "Category"] + bph_cols], on=["ServiceNo", "Direction"], how="left")
    # A service can pass the same stop pair twice on one direction (loops); count it once per link.
    links = links.drop_duplicates(["ServiceNo", "Direction", "from_stop", "to_stop"])
    agg = links.groupby(["from_stop", "to_stop"]).agg(
        services=("ServiceNo", "nunique"),
        service_list=("ServiceNo", lambda s: " ".join(sorted(set(s), key=lambda x: (len(x), x)))),
        link_km=("link_km", "median"),
        **{c: (c, "sum") for c in bph_cols},
    ).reset_index()
    stops = con.execute("select BusStopCode, RoadName, Description, Latitude, Longitude from stops").df()
    s = stops.set_index("BusStopCode")
    for end, col in (("from", "from_stop"), ("to", "to_stop")):
        agg = agg.join(s.add_prefix(f"{end}_"), on=col)
    agg["bus_km_am_peak"] = agg.AM_Peak_bph * agg.link_km
    return agg.sort_values(["AM_Peak_bph", "services"], ascending=False)


def road_corridors(agg, min_services=10):
    """Roll links up to roads: links whose two stops lie on the same road name."""
    same = agg[agg.from_RoadName == agg.to_RoadName]
    heavy = same[same.services >= min_services]
    return heavy.groupby("from_RoadName").agg(
        links=("from_stop", "size"),
        km=("link_km", "sum"),
        max_services=("services", "max"),
        mean_am_peak_bph=("AM_Peak_bph", "mean"),
        max_am_peak_bph=("AM_Peak_bph", "max"),
    ).sort_values("km", ascending=False).reset_index().rename(columns={"from_RoadName": "road"})


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB))
    agg = build(con)
    con.execute("create or replace table corridor_links as select * from agg")
    agg.to_csv(OUT / "corridor_links.csv", index=False)
    roads = road_corridors(agg)
    roads.to_csv(OUT / "corridor_roads.csv", index=False)
    print(f"{len(agg):,} directed links; {(agg.services >= 10).sum():,} carry 10+ services; "
          f"{(agg.AM_Peak_bph >= 60).sum():,} have 60+ scheduled buses/hour in the AM peak")
    print(agg.head(15)[["from_Description", "to_Description", "from_RoadName", "services", "AM_Peak_bph", "link_km"]]
          .round(1).to_string(index=False))
    print(roads.head(15).round(2).to_string(index=False))
