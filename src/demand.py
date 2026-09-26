"""Stop demand profiles and month-over-month surveillance from DataMall Passenger Volume by Bus Stops.

The monthly file sums tap-ins over every weekday (or every weekend/holiday) in the month, so months with different
day counts cannot be compared directly. Totals are divided by the number of days of each type, using the MOM 2026
public holiday list (the Monday after a Sunday holiday is counted as a holiday):
https://www.mom.gov.sg/employment-practices/public-holidays (checked 2026-09-26).

Surveillance: for each stop, average daily weekday tap-ins in the latest month vs a baseline month. A stop is flagged
when the log ratio is far from the network-wide shift (robust z-score on the median absolute deviation) and the stop
is busy enough for the change to matter, and the change relative to the network is at least MIN_EFFECT (so a
statistically unusual but small move is not flagged).
"""
import calendar
import datetime as dt
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
OUT = ROOT / "outputs"

HOLIDAYS_2026 = {dt.date(2026, 1, 1), dt.date(2026, 2, 17), dt.date(2026, 2, 18), dt.date(2026, 3, 21),
                 dt.date(2026, 4, 3), dt.date(2026, 5, 1), dt.date(2026, 5, 27), dt.date(2026, 5, 31),
                 dt.date(2026, 6, 1), dt.date(2026, 8, 9), dt.date(2026, 8, 10), dt.date(2026, 11, 8),
                 dt.date(2026, 11, 9), dt.date(2026, 12, 25)}


def day_counts(year_month):
    y, m = map(int, year_month.split("-"))
    days = [dt.date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]
    wd = sum(1 for d in days if d.weekday() < 5 and d not in HOLIDAYS_2026)
    return {"WEEKDAY": wd, "WEEKENDS/HOLIDAY": len(days) - wd}


def daily(con):
    v = con.execute("""select YEAR_MONTH, DAY_TYPE, cast(TIME_PER_HOUR as int) as "hour", PT_CODE as stop,
                              TOTAL_TAP_IN_VOLUME tap_in, TOTAL_TAP_OUT_VOLUME tap_out
                       from stop_volume where TIME_PER_HOUR between 0 and 23""").df()
    n = {(ym, t): c for ym in v.YEAR_MONTH.unique() for t, c in day_counts(ym).items()}
    v["days"] = [n[k] for k in zip(v.YEAR_MONTH, v.DAY_TYPE)]
    v["tap_in_per_day"] = v.tap_in / v.days
    v["tap_out_per_day"] = v.tap_out / v.days
    return v


def network_profile(v):
    p = v.groupby(["YEAR_MONTH", "DAY_TYPE", "hour"])[["tap_in_per_day", "tap_out_per_day"]].sum().reset_index()
    tot = v.groupby(["YEAR_MONTH", "DAY_TYPE"]).agg(tap_in=("tap_in", "sum"), days=("days", "first"),
                                                     tap_in_per_day=("tap_in_per_day", "sum")).reset_index()
    return p, tot


def stop_profile(v, month):
    w = v[(v.YEAR_MONTH == month) & (v.DAY_TYPE == "WEEKDAY")]
    piv = w.pivot_table(index="stop", columns="hour", values="tap_in_per_day", aggfunc="sum", fill_value=0)
    out = pd.DataFrame({"weekday_tap_in_per_day": piv.sum(axis=1)})
    out["am_peak_share"] = piv.reindex(columns=[7, 8], fill_value=0).sum(axis=1) / out.weekday_tap_in_per_day
    out["pm_peak_share"] = piv.reindex(columns=[17, 18, 19], fill_value=0).sum(axis=1) / out.weekday_tap_in_per_day
    out["peak_hour"] = piv.idxmax(axis=1)
    # Timing label only. PM-peak boarding covers job and school stops but also interchanges, where riders transfer
    # home from the MRT in the evening, so it is not read as "destination" without land-use data.
    out["boarding_pattern"] = np.select(
        [out.am_peak_share > out.pm_peak_share * 1.5, out.pm_peak_share > out.am_peak_share * 1.5],
        ["AM-peak boarding", "PM-peak boarding"], "balanced")
    return out.reset_index()


def surveillance(v, month, base, min_daily=300, z_cut=3.5, min_effect=0.25):
    w = v[v.DAY_TYPE == "WEEKDAY"].groupby(["stop", "YEAR_MONTH"]).tap_in_per_day.sum().unstack()
    s = w[[base, month]].dropna()
    s = s[(s[base] >= min_daily) | (s[month] >= min_daily)]
    lr = np.log(s[month] + 1) - np.log(s[base] + 1)
    med = lr.median()
    mad = (lr - med).abs().median() * 1.4826
    s = s.assign(log_ratio=lr, pct_change=np.expm1(lr), network_shift=np.expm1(med), robust_z=(lr - med) / mad)
    s["vs_network"] = np.expm1(lr - med)
    big = s.vs_network.abs() >= min_effect
    s["flag"] = np.where(big & (s.robust_z >= z_cut), "surge", np.where(big & (s.robust_z <= -z_cut), "drop", ""))
    return s.reset_index().sort_values("robust_z", key=abs, ascending=False)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB))
    v = daily(con)
    prof, tot = network_profile(v)
    prof.to_csv(OUT / "network_hourly_profile.csv", index=False)
    print(tot.round(0).to_string(index=False))
    stops = con.execute("select BusStopCode stop, Description, RoadName, Latitude, Longitude from stops").df()
    sp = stop_profile(v, "2026-08").merge(stops, on="stop", how="left")
    sp.to_csv(OUT / "stop_profile_2026-08.csv", index=False)
    print(sp.boarding_pattern.value_counts().to_string())
    print(sp.sort_values("weekday_tap_in_per_day", ascending=False).head(10)
          [["stop", "Description", "weekday_tap_in_per_day", "am_peak_share", "pm_peak_share", "boarding_pattern"]].round(2).to_string(index=False))
    for base in ["2026-07", "2026-02"]:
        s = surveillance(v, "2026-08", base).merge(stops, on="stop", how="left")
        s.to_csv(OUT / f"surveillance_2026-08_vs_{base}.csv", index=False)
        print(f"\n2026-08 vs {base}: {len(s):,} stops tested, network shift {s.network_shift.iloc[0]:+.1%}, "
              f"{(s.flag == 'surge').sum()} surges, {(s.flag == 'drop').sum()} drops")
        print(s[s.flag != ""].head(12)[["stop", "Description", "RoadName", base, "2026-08", "pct_change", "robust_z"]]
              .round(2).to_string(index=False))
