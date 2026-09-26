"""400 m bus stop coverage by subzone and planning area, weighted by resident population.

Catchment: a 400 m straight-line buffer around every stop served by at least one route (SVY21, EPSG:3414).
Population: SingStat GHS 2025 residents by subzone (table C020123), placed on housing land only (dasymetric).
Within each MP2019 subzone, residents are shared among the Master Plan 2019 residential land-use parcels in
proportion to parcel area x gross plot ratio (GPR). Landed parcels ("LND") take GPR 1.0; parcels marked "EVA"
(subject to evaluation) take the median GPR of their land-use class. Subzones with residents but no residential
parcel (for example worker dormitories on industrial land) fall back to an even spread over the subzone.
The even-spread figure is kept as a comparison column.
"""
import json
import re
from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
OUT = ROOT / "outputs"
RADIUS_M = 400
RESIDENTIAL = ["RESIDENTIAL", "RESIDENTIAL WITH COMMERCIAL AT 1ST STOREY", "COMMERCIAL & RESIDENTIAL",
               "RESIDENTIAL / INSTITUTION"]


def key(name):
    return re.sub(r"[^A-Z0-9]", "", str(name).upper())


def population():
    rows = json.loads((RAW / "singstat_C020123_ghs2025.json").read_text(encoding="utf-8"))["Data"]["row"]
    out = []
    for r in rows:
        if r["rowNo"].count(".") != 1:  # subzone rows are "2.1", planning-area totals "2", Singapore "1"
            continue
        total = next(c for c in r["columns"] if c["key"] == "Total")["columns"][0]["value"]
        out.append({"subzone": r["rowText"], "residents": 0 if total in ("-", "") else int(float(total))})
    return pd.DataFrame(out)


def housing_parcels(sz):
    lu = gpd.read_file(RAW / "mp2019_land_use.geojson")
    lu = lu[lu.LU_DESC.isin(RESIDENTIAL)].to_crs(3414)[["LU_DESC", "GPR", "geometry"]]
    gpr = pd.to_numeric(lu.GPR, errors="coerce")
    gpr = gpr.where(lu.GPR != "LND", 1.0)
    gpr = gpr.fillna(gpr.groupby(lu.LU_DESC).transform("median"))
    lu["weight"] = lu.area * gpr
    pts = gpd.GeoDataFrame(lu[["weight"]], geometry=lu.representative_point(), crs=3414)
    lu["sz"] = gpd.sjoin(pts, sz[["geometry"]], predicate="within", how="left").index_right.groupby(level=0).first()
    return lu.dropna(subset=["sz"])


def build(con):
    sz = gpd.read_file(RAW / "mp2019_subzone.geojson").to_crs(3414)
    pop = population()
    sz["k"] = sz.SUBZONE_N.map(key)
    pop["k"] = pop.subzone.map(key)
    sz = sz.merge(pop[["k", "residents"]], on="k", how="left")
    unmatched = sz.residents.isna().sum()
    sz["residents"] = sz.residents.fillna(0)

    stops = con.execute("""select s.BusStopCode, s.Latitude, s.Longitude from stops s
                           where s.BusStopCode in (select distinct BusStopCode from bus_routes)""").df()
    pts = gpd.GeoDataFrame(stops, geometry=gpd.points_from_xy(stops.Longitude, stops.Latitude), crs=4326).to_crs(3414)
    catch = pts.buffer(RADIUS_M).union_all() if hasattr(pts, "union_all") else pts.buffer(RADIUS_M).unary_union

    sz["area_km2"] = sz.area / 1e6
    sz["covered_km2"] = sz.geometry.intersection(catch).area / 1e6
    sz["area_covered"] = sz.covered_km2 / sz.area_km2
    sz["residents_covered_even"] = sz.residents * sz.area_covered

    hp = housing_parcels(sz)
    hp["in_catch"] = hp.geometry.intersection(catch).area / hp.area
    w = hp.groupby("sz").apply(lambda g: (g.weight * g.in_catch).sum() / g.weight.sum())
    sz["housing_parcels"] = hp.groupby("sz").size().reindex(sz.index, fill_value=0)
    sz["housing_share_covered"] = w.reindex(sz.index)
    sz["method"] = sz.housing_share_covered.notna().map({True: "dasymetric", False: "even spread"})
    sz["residents_covered"] = sz.residents * sz.housing_share_covered.fillna(sz.area_covered)
    sz["stops"] = gpd.sjoin(pts, sz[["geometry"]], predicate="within").groupby("index_right").size() \
        .reindex(sz.index, fill_value=0)

    pa = sz.groupby(["PLN_AREA_N", "REGION_N"]).agg(
        residents=("residents", "sum"), residents_covered=("residents_covered", "sum"),
        residents_covered_even=("residents_covered_even", "sum"),
        area_km2=("area_km2", "sum"), covered_km2=("covered_km2", "sum"), stops=("stops", "sum")).reset_index()
    pa["resident_coverage"] = pa.residents_covered / pa.residents.where(pa.residents > 0)
    pa["resident_coverage_even"] = pa.residents_covered_even / pa.residents.where(pa.residents > 0)
    pa["residents_outside_400m"] = pa.residents - pa.residents_covered
    pa["stops_per_10k_residents"] = pa.stops / pa.residents.where(pa.residents > 0) * 1e4
    return sz, pa, unmatched, len(pts)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB))
    sz, pa, unmatched, n = build(con)
    total_res = sz.residents.sum()
    print(f"{n:,} served stops; {len(sz)} subzones, {unmatched} without a population match; "
          f"{total_res:,.0f} residents; {sz.residents_covered.sum() / total_res:.1%} live within {RADIUS_M} m of a stop "
          f"on housing land ({sz.residents_covered_even.sum() / total_res:.1%} with an even spread); "
          f"{(sz.method == 'even spread').sum()} subzones with residents but no housing parcel: "
          f"{sz.loc[(sz.method == 'even spread'), 'residents'].sum():,.0f} residents")
    cols = ["SUBZONE_N", "PLN_AREA_N", "residents", "area_km2", "area_covered", "housing_parcels",
            "housing_share_covered", "method", "residents_covered", "residents_covered_even", "stops"]
    sz[cols].to_csv(OUT / "coverage_subzone.csv", index=False)
    sz[cols + ["geometry"]].to_crs(4326).to_file(OUT / "coverage_subzone.geojson", driver="GeoJSON")
    pa.sort_values("residents_outside_400m", ascending=False).to_csv(OUT / "coverage_planning_area.csv", index=False)
    print(pa[pa.residents >= 20000].sort_values("resident_coverage")
          [["PLN_AREA_N", "residents", "resident_coverage", "resident_coverage_even", "residents_outside_400m",
            "stops_per_10k_residents"]]
          .head(12).round(3).to_string(index=False))
    sz["outside"] = sz.residents - sz.residents_covered
    print(sz.sort_values("outside", ascending=False)[["SUBZONE_N", "PLN_AREA_N", "residents", "housing_share_covered", "area_covered", "outside"]]
          .head(10).round(2).to_string(index=False))
