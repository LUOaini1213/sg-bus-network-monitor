"""The Bus Arrival sample: stops on three shared trunk corridors, and their scheduled AM-peak headways.

    python src/busarrival_stops.py [--db data/processed/sgbus.duckdb]

Writes data/reference/busarrival_stops.csv (the stops the Bus Arrival collector polls) and
data/reference/busarrival_services.csv (every service that calls at them, with its scheduled AM-peak headway band).

The corridors come from the shared-corridor analysis (outputs/corridor_links.csv, outputs/corridor_roads.csv):
a stop is in the sample if it is an end of a directed link carrying 10 or more services whose stop lies on the
corridor road. Marine Parade Rd has the most kilometres of 10+-service links (6.0 km, 19 stops), Telok Blangah Rd
the third most (4.4 km, 13 stops), and Orchard Rd carries the busiest single link in the network (28 services,
about 162 scheduled buses an hour; 9 stops). Yio Chu Kang Rd, second by length, is left out to keep the sample at
41 stops, which is 41 Bus Arrival calls a sweep (see src/busarrival.py for the call budget).
"""
import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "reference"
CORRIDORS = ["Marine Parade Rd", "Telok Blangah Rd", "Orchard Rd"]
MIN_SERVICES = 10


def corridor_stops(links, corridors=CORRIDORS, min_services=MIN_SERVICES):
    """Stops at either end of a 10+-service link, where that stop's own road is the corridor road."""
    heavy = links[links.services >= min_services]
    rows = []
    for road in corridors:
        for end in ("from", "to"):
            on = heavy[heavy[f"{end}_RoadName"] == road]
            rows.append(pd.DataFrame({
                "corridor": road, "BusStopCode": on[f"{end}_stop"], "Description": on[f"{end}_Description"],
                "Latitude": on[f"{end}_Latitude"], "Longitude": on[f"{end}_Longitude"], "services": on.services}))
    s = pd.concat(rows, ignore_index=True)
    s = (s.groupby(["corridor", "BusStopCode", "Description", "Latitude", "Longitude"], as_index=False)
         .services.max().rename(columns={"services": "max_link_services"}))
    return s.sort_values(["corridor", "BusStopCode"]).reset_index(drop=True)


def headway_minutes(band):
    """Scheduled headway from a BusServices band: '08-12' -> 10.0, '10' -> 10.0, '-' or '' -> None."""
    import re
    nums = [int(n) for n in re.findall(r"\d+", str(band or "")) if int(n) > 0]
    return sum(nums[:2]) / len(nums[:2]) if nums else None


def main(db):
    import duckdb
    links = pd.read_csv(ROOT / "outputs" / "corridor_links.csv", dtype={"from_stop": str, "to_stop": str})
    stops = corridor_stops(links)
    con = duckdb.connect(str(db), read_only=True)
    con.register("sample", stops[["BusStopCode"]])
    svc = con.execute("""
        select r.BusStopCode, r.ServiceNo, r.Direction, r.StopSequence, s.Category,
               s.AM_Peak_Freq, s.AM_Offpeak_Freq, s.PM_Peak_Freq, s.PM_Offpeak_Freq
        from bus_routes r join sample using (BusStopCode)
        join bus_services s on s.ServiceNo = r.ServiceNo and s.Direction = r.Direction
        order by r.BusStopCode, r.ServiceNo, r.Direction, r.StopSequence""").df()
    for band in ("AM_Peak", "AM_Offpeak", "PM_Peak", "PM_Offpeak"):
        svc[f"{band.lower()}_headway_min"] = svc[f"{band}_Freq"].map(headway_minutes)
    REF.mkdir(parents=True, exist_ok=True)
    stops.to_csv(REF / "busarrival_stops.csv", index=False)
    svc.to_csv(REF / "busarrival_services.csv", index=False)
    print(stops.groupby("corridor").size().to_string())
    print(f"{len(stops)} stops, {svc[['BusStopCode', 'ServiceNo']].drop_duplicates().shape[0]} stop-services")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "data" / "processed" / "sgbus.duckdb"))
    main(ap.parse_args().db)
