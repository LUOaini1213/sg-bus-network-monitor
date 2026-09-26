"""Evidence table for the stops flagged in August 2026 against the median of February, June and July.

For each flagged stop it collects what the data can show on its own, before any outside explanation is attached:
  - weekday boardings per day in every month (is it a one-month blip, a step, or a new stop?)
  - bus services added or removed at the stop between the DataMall BusRoutes pulls of 2026-04-22
    (data/reference/busroutes_20260422.csv.gz) and 2026-09-26
  - where the stop's trips go (OD, weekday) and which destinations changed most from July to August
  - a first-pass category from those facts; docs/ANOMALIES.md records the categories checked against official sources
"""
import json
import re
from pathlib import Path

import duckdb
import pandas as pd

from demand import daily

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "datamall"
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
OUT = ROOT / "outputs"
CAMPUS = re.compile(r"NUS|NTU|SMU|Nanyang Dr|Nanyang Ave|Nanyang Cres|Kent Ridge|UTown|Hall \d|Sch of|Academic Bldg|SDE\d|"
                    r"Lee Wee Nam|NIE Blk|SPMS|Poly\b|ITE Coll|Architecture Dr|Information Technology|Yusof Ishak|"
                    r"Coll of Design|Lien Ying Chow|Raffles Hall", re.I)

# City Direct services renumbered on 2026-06-15 (data/reference/network_events_2026.csv): same route, new number.
RENUMBERED = {"513": "646", "868E": "647", "951E": "648", "982E": "649", "850E": "650"}


def service_diff(before, after):
    """Services added and removed at a stop, treating renumbered services as unchanged."""
    before = {RENUMBERED.get(s, s) for s in before}
    return sorted(after - before), sorted(before - after)


def classify(r):
    """First-pass category for one flagged stop (a row of the evidence table)."""
    feb, jun, jul, aug = (r[f"wd_2026-0{m}"] for m in (2, 6, 7, 8))
    added = set(str(r.services_added or "").split()) - {"nan"}
    removed = set(str(r.services_removed or "").split()) - {"nan"}
    if r.flag == "drop" and len(removed) > len(added):
        return "services withdrawn or rerouted away"
    if r.flag == "surge" and added:
        return "services added or rerouted here"
    if added or removed:
        return "service change (direction unclear)"
    if CAMPUS.search(f"{r.Description} {r.RoadName}"):
        return "academic term"
    if r.near_campus and jun < 0.8 * aug and jul < 0.8 * aug:
        return "academic term (within 600 m of a campus)"
    # Weekday school-term shape: June (school holidays) well below February, August back near February.
    if jun < 0.8 * feb and abs(aug / feb - 1) <= 0.2:
        return "school-term seasonality (June holidays in the baseline)"
    if min(jul, aug) >= 1.4 * max(feb, jun):
        return "sustained step up since July"
    return "unexplained"


def routes(path):
    path = Path(path)
    if path.suffix == ".gz":
        df = pd.read_csv(path, dtype={"BusStopCode": str, "ServiceNo": str})
    else:
        rows = json.loads(path.read_text(encoding="utf-8"))
        df = pd.DataFrame(rows["value"] if isinstance(rows, dict) else rows)
    df["BusStopCode"] = df.BusStopCode.astype(str).str.zfill(5)
    return df.groupby("BusStopCode").ServiceNo.apply(set)


def main():
    con = duckdb.connect(str(DB))
    s = pd.read_csv(OUT / "surveillance_2026-08_vs_baseline_median.csv", dtype={"stop": str})
    f = s[s.flag.isin(["surge", "drop"])].copy()
    v = daily(con)
    series = (v[v.DAY_TYPE == "WEEKDAY"].groupby(["stop", "YEAR_MONTH"]).tap_in_per_day.sum().unstack().round(0))
    f = f.join(series.add_prefix("wd_"), on="stop")

    # The April pull is kept in data/reference because DataMall only serves the current network.
    apr = routes(ROOT / "data" / "reference" / "busroutes_20260422.csv.gz")
    sep = routes(sorted(RAW.glob("BusRoutes_2026092*.json"))[-1])
    diffs = [service_diff(apr.get(k, set()), sep.get(k, set())) for k in f.stop]
    f["services_added"] = [" ".join(a) for a, _ in diffs]
    f["services_removed"] = [" ".join(r) for _, r in diffs]
    ev = pd.read_csv(ROOT / "data" / "reference" / "network_events_2026.csv", dtype=str)
    by_stop = {}
    for e in ev.dropna(subset=["stops"]).itertuples():
        for st in e.stops.split():
            by_stop.setdefault(st, []).append(f"{e.event_id} ({e.date_from})")
    f["verified_event"] = f.stop.map(lambda k: "; ".join(by_stop.get(k, [])))

    od = pd.read_csv(OUT / "od_flagged_stops.csv", dtype={"stop": str})
    top = od.sort_values("change", key=abs, ascending=False).groupby("stop").head(2)
    desc = {k: "; ".join(f"{r.dest_pa.title()} {r.trips_jul:.0f}->{r.trips_aug:.0f}" for r in g.itertuples())
            for k, g in top.groupby("stop")}
    f["od_biggest_changes"] = f.stop.map(desc)

    # Stops within 600 m of a stop whose name marks a university, polytechnic or ITE campus.
    xy = con.execute("select BusStopCode, Latitude, Longitude from stops").df().set_index("BusStopCode")
    names = con.execute("select BusStopCode, Description, RoadName from stops").df()
    campus = names[[bool(CAMPUS.search(f"{a} {b}")) for a, b in zip(names.Description, names.RoadName)]].BusStopCode
    cxy = xy.loc[campus]
    def near_campus(stop, m=600):
        lat, lon = xy.loc[stop]
        d = ((cxy.Latitude - lat) ** 2 + ((cxy.Longitude - lon) * 0.99995) ** 2) ** 0.5 * 111_320
        return bool((d <= m).any())
    f["near_campus"] = [near_campus(k) for k in f.stop]
    f["category"] = f.apply(classify, axis=1)
    cols = ["stop", "Description", "RoadName", "flag", "baseline_median", "2026-08", "pct_change", "robust_z",
            "wd_2026-02", "wd_2026-06", "wd_2026-07", "wd_2026-08", "services_added", "services_removed",
            "od_biggest_changes", "near_campus", "category", "verified_event"]
    f = f[cols].sort_values(["category", "robust_z"])
    f.to_csv(OUT / "anomaly_evidence.csv", index=False)
    print(f.category.value_counts().to_string())
    # Stops with August boardings but no February or June record have no baseline, so the surveillance cannot test them.
    new = series[series["2026-02"].isna() & series["2026-06"].isna() & (series["2026-08"] >= 100)].copy()
    new["services_now"] = [" ".join(sorted(sep.get(k, set()))) for k in new.index]
    new["services_apr"] = [" ".join(sorted(apr.get(k, set()))) for k in new.index]
    names = con.execute("select BusStopCode, Description, RoadName from stops").df().set_index("BusStopCode")
    new = new.join(names).reset_index().rename(columns={"index": "stop"})
    new.to_csv(OUT / "new_stops_2026.csv", index=False)
    print(f"new stops with 100+ weekday boardings in August and no earlier record: {len(new)}")
    print(new.to_string(index=False))
    return f


if __name__ == "__main__":
    main()
