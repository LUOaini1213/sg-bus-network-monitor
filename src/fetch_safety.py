"""Refresh the public road-safety and exposure tables used by safety.py (no key needed).

Writes data/reference/safety/:
  singstat_bus_casualties.csv   SingStat M651281: bus occupants killed / injured, by year (persons)
  singstat_bus_ridership.csv    SingStat M651351: average daily public bus ridership (thousand rides)
  lta_bus_km_2004_2014.csv      data.gov.sg (LTA): average daily distance travelled by bus, 2004-2014 (km)

The Traffic Police vehicle tables (spf_vehicles_involved.csv, spf_accidents.csv) and the bus fleet table come from
PDF publications; they were transcribed once with row-sum checks and are committed with their table numbers.
"""
import io
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "reference" / "safety"
SINGSTAT = "https://tablebuilder.singstat.gov.sg/api/table/tabledata/{}"
DATAGOV = "https://api-open.data.gov.sg/v1/public/api/datasets/{}/poll-download"
HDR = {"User-Agent": "Mozilla/5.0 (sg-bus-network-monitor)"}


def singstat_rows(table):
    d = requests.get(SINGSTAT.format(table), headers=HDR, timeout=60).json()["Data"]["row"]
    rows = []
    for r in d:
        for c in r["columns"]:
            rows.append({"series": r["seriesNo"], "row": r["rowText"], "year": int(c["key"]), "value": c["value"]})
    return pd.DataFrame(rows)


def bus_casualties(rows):
    """Rows 1.x are fatalities and 2.x injured; 'Buses' is the bus-occupant road-user group."""
    b = rows[rows.row == "Buses"].copy()
    b["kind"] = b.series.str[0].map({"1": "killed", "2": "injured"})
    b["value"] = pd.to_numeric(b.value, errors="coerce")
    return b.pivot_table(index="year", columns="kind", values="value").reset_index()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cas = bus_casualties(singstat_rows("M651281"))
    cas.to_csv(OUT / "singstat_bus_casualties.csv", index=False, lineterminator="\n")
    rid = singstat_rows("M651351")
    rid = rid[rid.row == "Average Daily Ridership - Public Bus"][["year", "value"]]
    rid = rid.assign(avg_daily_rides_thousand=pd.to_numeric(rid.value, errors="coerce")).drop(columns="value")
    rid.dropna().to_csv(OUT / "singstat_bus_ridership.csv", index=False, lineterminator="\n")
    url = requests.get(DATAGOV.format("d_120bba76c28dd4706e0cf85693827e64"), headers=HDR, timeout=60).json()
    km = pd.read_csv(io.StringIO(requests.get(url["data"]["url"], timeout=60).text))
    km.to_csv(OUT / "lta_bus_km_2004_2014.csv", index=False, lineterminator="\n")
    print(f"casualties {cas.year.min()}-{cas.year.max()}, ridership {int(rid.year.min())}-{int(rid.year.max())}, "
          f"bus-km {km.iloc[0, 0]}-{km.iloc[-1, 0]}")


if __name__ == "__main__":
    main()
