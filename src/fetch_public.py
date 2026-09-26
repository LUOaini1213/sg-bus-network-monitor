"""Fetch keyless public inputs: OSM bus stops (Overpass), URA Master Plan 2019 planning area and subzone boundaries
(data.gov.sg) and SingStat GHS 2025 resident population by planning area / subzone (Table Builder C020123)."""
import json, pathlib, requests
RAW = pathlib.Path(__file__).resolve().parents[1] / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)
HDR = {"User-Agent": "sg-bus-network-monitor/0.1 (portfolio research; contact via GitHub LUOaini1213)"}

def osm_bus_stops():
    q = """[out:json][timeout:120];
    area["ISO3166-1"="SG"][admin_level=2]->.sg;
    node["highway"="bus_stop"](area.sg);
    out body;"""
    for url in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
        r = requests.post(url, data={"data": q}, headers=HDR, timeout=180)
        if r.ok:
            break
    r.raise_for_status()
    (RAW / "osm_bus_stops.json").write_text(r.text, encoding="utf-8")
    return len(r.json()["elements"])

def data_gov_geojson(ds, name):
    meta = requests.get(f"https://api-open.data.gov.sg/v1/public/api/datasets/{ds}/poll-download", timeout=60, headers=HDR).json()
    g = requests.get(meta["data"]["url"], timeout=120, headers=HDR)
    (RAW / name).write_bytes(g.content)
    return len(g.json()["features"])

def planning_areas():
    return data_gov_geojson("d_4765db0e87b9c86336792efe8a1f7a66", "mp2019_planning_area.geojson")  # Planning Area (No Sea)

def subzones():
    return data_gov_geojson("d_8594ae9ff96d0c708bc2af633048edfb", "mp2019_subzone.geojson")  # Subzone Boundary (No Sea)

def land_use():
    return data_gov_geojson("d_90d86daa5bfaa371668b84fa5f01424f", "mp2019_land_use.geojson")  # Master Plan 2019 Land Use

def population():
    r = requests.get("https://tablebuilder.singstat.gov.sg/api/table/tabledata/C020123", params={"limit": 5000},
                     headers=HDR, timeout=120)
    r.raise_for_status()
    (RAW / "singstat_C020123_ghs2025.json").write_bytes(r.content)
    return len(r.json()["Data"]["row"])

if __name__ == "__main__":
    print("osm stops", osm_bus_stops())
    print("planning areas", planning_areas())
    print("subzones", subzones())
    print("land use polygons", land_use())
    print("population rows", population())
