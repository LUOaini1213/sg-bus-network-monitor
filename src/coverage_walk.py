"""400 m bus stop coverage measured along the walking network instead of in a straight line.

Every served stop is snapped to its nearest walking-network node. One Dijkstra run from a virtual source joined to
all stop nodes, with each join weighted by that stop's snap distance, gives the walking distance from every node to
its nearest stop, capped at 400 m. A housing parcel (MP2019 residential land use) counts as covered when the
distance from its representative point to its nearest node, plus that node's walking distance, is at most 400 m.
Residents are placed on parcels as in coverage.py (subzone population x parcel area x plot ratio).

For a like-for-like comparison, the straight-line figure is recomputed on the same parcel points here.
Using one point per parcel understates access for very large parcels whose edges are nearer a stop.
"""
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

from coverage import RAW, housing_parcels, key, population

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
DB = PROC / "sgbus.duckdb"
OUT = ROOT / "outputs"
RADIUS_M, MAX_SNAP_M, MIN_COMPONENT, DETOUR = 400, 150, 1000, 1.3


def walk_distance_to_stop(stop_xy):
    nodes = pd.read_parquet(PROC / "walk_nodes.parquet")
    edges = pd.read_parquet(PROC / "walk_edges.parquet")
    idx = pd.Series(np.arange(len(nodes)), index=nodes.node)
    u, v = idx.loc[edges.u].to_numpy(), idx.loc[edges.v].to_numpy()
    n = len(nodes)
    # OSM has thousands of footpath fragments that are not joined to the street network (paths inside estates,
    # podiums, parks). Snapping a stop or a home to one of them makes it look unreachable, so points are snapped only
    # to nodes of components with at least MIN_COMPONENT nodes (mainland Singapore, Sentosa and a few others).
    _, lab = connected_components(coo_matrix((np.ones(len(u)), (u, v)), shape=(n, n)), directed=False)
    keep = np.flatnonzero(np.bincount(lab)[lab] >= MIN_COMPONENT)
    kd = cKDTree(nodes[["x", "y"]].to_numpy()[keep])
    snap_d, k = kd.query(stop_xy)
    snap_i = keep[k]
    ok = snap_d <= MAX_SNAP_M
    src = n  # virtual source node
    rows = np.concatenate([u, v, np.full(ok.sum(), src)])
    cols = np.concatenate([v, u, snap_i[ok]])
    w = np.concatenate([edges.length_m.to_numpy()] * 2 + [np.maximum(snap_d[ok], 1e-3)])
    g = coo_matrix((w, (rows, cols)), shape=(n + 1, n + 1)).tocsr()
    dist = dijkstra(g, directed=True, indices=src, limit=RADIUS_M + 1)
    tree = _Snap(kd, keep)
    return tree, dist[:n], int((~ok).sum())


class _Snap:
    """Nearest-node lookup restricted to the large components; returns indices into the full node table."""
    def __init__(self, kd, keep):
        self.kd, self.keep = kd, keep

    def query(self, xy):
        d, k = self.kd.query(xy)
        return d, self.keep[k]


def main():
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB))
    stops = con.execute("""select BusStopCode, Latitude, Longitude from stops
                           where BusStopCode in (select distinct BusStopCode from bus_routes)""").df()
    pts = gpd.GeoDataFrame(stops, geometry=gpd.points_from_xy(stops.Longitude, stops.Latitude), crs=4326).to_crs(3414)
    stop_xy = np.column_stack([pts.geometry.x, pts.geometry.y])
    tree, dist, unsnapped = walk_distance_to_stop(stop_xy)

    sz = gpd.read_file(RAW / "mp2019_subzone.geojson").to_crs(3414)
    pop = population()
    sz["k"] = sz.SUBZONE_N.map(key)
    pop["k"] = pop.subzone.map(key)
    sz = sz.merge(pop[["k", "residents"]], on="k", how="left").fillna({"residents": 0})
    hp = housing_parcels(sz)
    rp = hp.representative_point()
    pxy = np.column_stack([rp.x, rp.y])
    d_node, i_node = tree.query(pxy)
    hp["walk_m"] = dist[i_node] + d_node
    hp["walk_ok"] = hp.walk_m <= RADIUS_M
    d_line, _ = cKDTree(stop_xy).query(pxy)
    hp["line_ok"] = d_line <= RADIUS_M
    hp["line13_ok"] = d_line <= RADIUS_M / DETOUR  # straight line with a planning detour factor

    agg = hp.groupby("sz").apply(lambda g: pd.Series({
        "w": g.weight.sum(), "w_walk": (g.weight * g.walk_ok).sum(), "w_line": (g.weight * g.line_ok).sum(),
        "w_line13": (g.weight * g.line13_ok).sum()}))
    sz = sz.join(agg)
    has = sz.w > 0
    sz["share_walk"] = np.where(has, sz.w_walk / sz.w, np.nan)
    sz["share_line"] = np.where(has, sz.w_line / sz.w, np.nan)
    sz["res_walk"] = sz.residents * sz.share_walk.fillna(0)
    sz["res_line"] = sz.residents * sz.share_line.fillna(0)
    sz["res_line13"] = sz.residents * np.where(has, sz.w_line13 / sz.w, 0)
    counted = sz[has]
    total = counted.residents.sum()
    print(f"{len(stops):,} stops, {unsnapped} more than {MAX_SNAP_M} m from the walking network; "
          f"{len(hp):,} housing parcels; residents on housing parcels {total:,.0f} of {sz.residents.sum():,.0f}")
    print(f"within 400 m: straight line {counted.res_line.sum() / total:.1%}; straight line / {DETOUR} "
          f"({RADIUS_M / DETOUR:.0f} m) {counted.res_line13.sum() / total:.1%}; OSM walking network {counted.res_walk.sum() / total:.1%}")
    pd.DataFrame([{"measure": "straight line 400 m", "share": counted.res_line.sum() / total},
                  {"measure": f"straight line {RADIUS_M / DETOUR:.0f} m (detour factor {DETOUR})", "share": counted.res_line13.sum() / total},
                  {"measure": "OSM walking network 400 m", "share": counted.res_walk.sum() / total}])         .to_csv(OUT / "coverage_measures.csv", index=False)
    pa = counted.groupby("PLN_AREA_N").agg(residents=("residents", "sum"), res_line=("res_line", "sum"),
                                          res_line13=("res_line13", "sum"), res_walk=("res_walk", "sum")).reset_index()
    pa["coverage_line13"] = pa.res_line13 / pa.residents
    pa["coverage_line"] = pa.res_line / pa.residents
    pa["coverage_walk"] = pa.res_walk / pa.residents
    pa["gap_pts"] = (pa.coverage_line - pa.coverage_walk) * 100
    pa["residents_outside_walk"] = pa.residents - pa.res_walk
    pa = pa.sort_values("coverage_walk")
    pa.to_csv(OUT / "coverage_walk_planning_area.csv", index=False)
    sz[["SUBZONE_N", "PLN_AREA_N", "residents", "share_line", "share_walk", "res_line", "res_walk"]] \
        .to_csv(OUT / "coverage_walk_subzone.csv", index=False)
    print(pa[pa.residents >= 20000].head(12).round(3).to_string(index=False))
    print(pa[pa.residents >= 20000].sort_values("gap_pts", ascending=False).head(8)
          [["PLN_AREA_N", "coverage_line", "coverage_walk", "gap_pts"]].round(3).to_string(index=False))
    return pa


if __name__ == "__main__":
    main()
