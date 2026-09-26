"""Build a DuckDB database from DataMall + OSM inputs and run data-quality checks.

Inputs (data/raw): DataMall BusRoutes, BusStops, BusServices and Passenger Volume by Bus Stops
(2026-02, 2026-06, 2026-07, 2026-08), OSM bus stops (Overpass, for a cross-check), URA MP2019 planning areas.
Outputs: data/processed/sgbus.duckdb, outputs/data_quality.csv
"""
import json, pathlib
import duckdb, pandas as pd, geopandas as gpd

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW, PROC, OUT = ROOT / "data/raw", ROOT / "data/processed", ROOT / "outputs"
PROC.mkdir(exist_ok=True, parents=True); OUT.mkdir(exist_ok=True)

def _latest(pattern):
    return sorted((RAW / "datamall").glob(pattern))[-1]

def load():
    routes = pd.DataFrame(json.loads(_latest("BusRoutes_*.json").read_text()))
    routes["BusStopCode"] = routes["BusStopCode"].astype(str).str.zfill(5)
    services = pd.DataFrame(json.loads(_latest("BusServices_*.json").read_text()))
    stops = pd.DataFrame(json.loads(_latest("BusStops_*.json").read_text()))
    stops["BusStopCode"] = stops["BusStopCode"].astype(str).str.zfill(5)
    vols = []
    for f in sorted((RAW / "datamall").glob("transport_node_bus_*.csv")):
        v = pd.read_csv(f, dtype={"PT_CODE": str})
        v["PT_CODE"] = v["PT_CODE"].str.zfill(5)
        vols.append(v)
    vol = pd.concat(vols, ignore_index=True)
    osm = json.loads((RAW / "osm_bus_stops.json").read_text(encoding="utf-8"))["elements"]
    osm = pd.DataFrame([{"BusStopCode": e["tags"].get("ref"), "osm_lat": e["lat"], "osm_lon": e["lon"],
                         "shelter": e["tags"].get("shelter")} for e in osm if e.get("tags", {}).get("ref")])
    osm = osm[osm.BusStopCode.str.fullmatch(r"\d{5}", na=False)].drop_duplicates("BusStopCode")
    stops = stops.merge(osm, on="BusStopCode", how="left")
    pa = gpd.read_file(RAW / "mp2019_planning_area.geojson")
    return routes, services, vol, stops, pa

def links(routes):
    r = routes.sort_values(["ServiceNo", "Direction", "StopSequence"]).copy()
    r["to_stop"] = r.groupby(["ServiceNo", "Direction"])["BusStopCode"].shift(-1)
    r["to_dist"] = r.groupby(["ServiceNo", "Direction"])["Distance"].shift(-1)
    r = r.dropna(subset=["to_stop"])
    r["link_km"] = r["to_dist"] - r["Distance"]
    return r.rename(columns={"BusStopCode": "from_stop"})[["ServiceNo", "Direction", "StopSequence", "from_stop", "to_stop", "link_km"]]

def quality(routes, services, vol, stops, lk):
    q = []
    def add(check, table, n_bad, n_total, note=""):
        q.append({"check": check, "table": table, "failing_rows": int(n_bad), "total_rows": int(n_total),
                  "pass_rate": round(1 - n_bad / n_total, 4) if n_total else None, "note": note})
    add("primary key unique (ServiceNo, Direction, StopSequence)", "bus_routes",
        routes.duplicated(["ServiceNo", "Direction", "StopSequence"]).sum(), len(routes))
    seq = routes.sort_values(["ServiceNo", "Direction", "StopSequence"]).groupby(["ServiceNo", "Direction"])["StopSequence"]
    gaps = (seq.diff().fillna(1) != 1).sum()
    add("stop sequence continuous (step = 1)", "bus_routes", gaps, len(routes))
    add("cumulative distance non-decreasing along route", "bus_links", (lk.link_km < 0).sum(), len(lk))
    add("link length plausible (0 < km <= 5)", "bus_links", (~lk.link_km.between(0.0001, 5)).sum(), len(lk),
        "0 km = consecutive stops sharing a location; >5 km = non-stop sections of express and City Direct services (e.g. 646-649)")
    codes = set(routes.BusStopCode)
    add("route stop exists in BusStops master", "bus_routes", len(codes - set(stops.BusStopCode)), len(codes), "unique stop codes")
    both = stops.dropna(subset=["osm_lat"])
    d_m = ((both.Latitude - both.osm_lat) ** 2 + ((both.Longitude - both.osm_lon) * 0.99995) ** 2) ** 0.5 * 111_320
    add("LTA and OSM stop positions agree within 50 m", "stops", (d_m > 50).sum(), len(both), f"median gap {d_m.median():.1f} m")
    add("stop has non-zero coordinates", "stops", ((stops.Latitude == 0) | (stops.Longitude == 0)).sum(), len(stops))
    svc_keys = set(zip(services.ServiceNo, services.Direction))
    rt_keys = set(zip(routes.ServiceNo, routes.Direction))
    add("route service exists in BusServices", "bus_routes", len(rt_keys - svc_keys), len(rt_keys), "service-direction pairs")
    add("AM peak frequency present", "bus_services", (services.AM_Peak_Freq.isin(["", "-", None])).sum(), len(services))
    add("volume stop code exists in route network", "stop_volume", (~vol.PT_CODE.isin(codes)).sum(), len(vol))
    add("volume non-negative", "stop_volume", ((vol.TOTAL_TAP_IN_VOLUME < 0) | (vol.TOTAL_TAP_OUT_VOLUME < 0)).sum(), len(vol))
    add("hour within 0-23", "stop_volume", (~vol.TIME_PER_HOUR.between(0, 23)).sum(), len(vol))
    add("stop inside Singapore bounding box", "stops",
        (~(stops.Latitude.between(1.15, 1.48) & stops.Longitude.between(103.59, 104.10))).sum(), len(stops),
        "expected: 46239 Larkin Terminal, Johor Bahru (cross-border service 170)")
    return pd.DataFrame(q)

if __name__ == "__main__":
    routes, services, vol, stops, pa = load()
    lk = links(routes)
    qc = quality(routes, services, vol, stops, lk)
    qc.to_csv(OUT / "data_quality.csv", index=False)
    con = duckdb.connect(str(PROC / "sgbus.duckdb"))
    for name, df in {"bus_routes": routes, "bus_links": lk, "stop_volume": vol, "stops": stops, "bus_services": services}.items():
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM df")
    pa[["PLN_AREA_N", "REGION_N"]].to_csv(PROC / "planning_areas.csv", index=False) if "PLN_AREA_N" in pa.columns else None
    print(qc.to_string(index=False))
    print({t: con.execute(f"select count(*) from {t}").fetchone()[0] for t in ["bus_routes", "bus_links", "stop_volume", "stops", "bus_services"]})
    print("planning area columns:", list(pa.columns)[:8])
