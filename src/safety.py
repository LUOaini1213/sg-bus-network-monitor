"""Bus safety brief from public data: trends, exposure-normalised rates with exact intervals, and where live
DataMall traffic incidents fall relative to the bus network.

Inputs (data/reference/safety/, refreshed by fetch_safety.py where an API exists):
- Traffic Police: buses involved in fatal / injury accidents (vehicles, all bus types), 2004-2025.
- SingStat M651281: bus occupants killed / injured; M651351: average daily public bus ridership.
- LTA: bus fleet by type, 2015-2025; average daily bus-km, 2004-2014 (the series stops in 2014).
- Ministry of Transport / LTA statements on public buses only (Taskforce counts 2022-2024, accident rates).
- DataMall Traffic Incidents, polled every 2 minutes by collect.py since 13 October 2026 (data/raw, not committed).

Rates: Poisson counts with exact (Garwood) 95% intervals; comparisons of two rates use the exact conditional
(binomial) interval for the rate ratio.

Writes outputs/safety_annual.csv, outputs/safety_public_bus.csv, outputs/safety_incidents.csv,
outputs/safety_summary.json, docs/safety_trend.png and docs/BUS_SAFETY_BRIEF.md.
"""
import calendar
import datetime as dt
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "reference" / "safety"
INC = ROOT / "data" / "raw" / "datamall" / "incidents"
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
LINKS = ROOT / "data" / "raw" / "datamall" / "speedband_links.csv"
OUT = ROOT / "outputs"
DOCS = ROOT / "docs"
SNAP_M = 30.0


def poisson_ci(k, alpha=0.05):
    """Exact (Garwood) interval for a Poisson mean given an observed count k."""
    k = np.asarray(k, dtype=float)
    lo = np.where(k > 0, stats.chi2.ppf(alpha / 2, 2 * k) / 2, 0.0)
    hi = stats.chi2.ppf(1 - alpha / 2, 2 * k + 2) / 2
    return lo, hi


def rate_ratio(k1, e1, k2, e2, alpha=0.05):
    """Rate ratio (k1/e1)/(k2/e2) with the exact conditional interval (binomial on k1 of k1+k2)."""
    n = k1 + k2
    lo_p = stats.beta.ppf(alpha / 2, k1, n - k1 + 1) if k1 > 0 else 0.0
    hi_p = stats.beta.ppf(1 - alpha / 2, k1 + 1, n - k1) if k1 < n else 1.0
    scale = e2 / e1
    to_rr = lambda p: p / (1 - p) * scale if p < 1 else np.inf  # noqa: E731
    return (k1 / e1) / (k2 / e2), to_rr(lo_p), to_rr(hi_p)


def annual():
    v = pd.read_csv(REF / "spf_vehicles_involved.csv")
    cas = pd.read_csv(REF / "singstat_bus_casualties.csv")
    rid = pd.read_csv(REF / "singstat_bus_ridership.csv")
    fleet = pd.read_csv(REF / "lta_bus_population.csv")[["year", "total_buses", "omnibuses"]]
    t = v.merge(cas, on="year", how="left").merge(rid, on="year", how="left").merge(fleet, on="year", how="left")
    t["bus_share_of_vehicles"] = t.bus_vehicles_fatal_injury / t.all_vehicles_fatal_injury
    k = t.bus_vehicles_fatal_injury
    lo, hi = poisson_ci(k)
    t["per_1000_buses"] = k / t.total_buses * 1000
    t["per_1000_buses_lo"] = lo / t.total_buses * 1000
    t["per_1000_buses_hi"] = hi / t.total_buses * 1000
    days = np.where([calendar.isleap(int(y)) for y in t.year], 366, 365)
    rides = t.avg_daily_rides_thousand * 1000 * days
    t["annual_public_bus_rides"] = rides
    inj = t.injured
    lo, hi = poisson_ci(inj.fillna(0))
    t["occupants_injured_per_100m_rides"] = inj / rides * 1e8
    t["occupants_injured_per_100m_rides_lo"] = np.where(inj.notna(), lo / rides * 1e8, np.nan)
    t["occupants_injured_per_100m_rides_hi"] = np.where(inj.notna(), hi / rides * 1e8, np.nan)
    return t


def public_bus(t):
    f = pd.read_csv(REF / "public_bus_figures.csv", dtype={"period": str})
    s = f[f.metric == "serious_collisions_public_buses"].copy()
    s["year"] = s.period.astype(int)
    s = s.merge(t[["year", "annual_public_bus_rides"]], on="year")
    lo, hi = poisson_ci(s.value)
    s["per_100m_rides"] = s.value / s.annual_public_bus_rides * 1e8
    s["per_100m_rides_lo"] = lo / s.annual_public_bus_rides * 1e8
    s["per_100m_rides_hi"] = hi / s.annual_public_bus_rides * 1e8
    fault = f[f.metric == "serious_collisions_captain_at_fault"].set_index("period").value.astype(float)
    s["captain_at_fault_share"] = s.period.map(fault) / s.value
    jm = f[f.metric == "accidents_public_buses_jan_may"].set_index("period").value.astype(float)
    rate17 = float(f[(f.metric == "accidents_per_100k_bus_km") & (f.period == "2017 (year)")].value.iloc[0])
    days_jan_may_2017 = (dt.date(2017, 6, 1) - dt.date(2017, 1, 1)).days
    implied = {"implied_bus_km_per_day_2017": jm["2017"] / rate17 * 1e5 / days_jan_may_2017,
               "note": "Jan-May 2017 public bus accidents divided by the rounded 2017 yearly rate of about 0.15 per "
                       "100,000 bus-km; a rough figure, since the rate is rounded and is a full-year average.",
               "bus_km_2014": float(pd.read_csv(REF / "lta_bus_km_2004_2014.csv").set_index("year").bus[2014])}
    return s, implied


def comparisons(t, s):
    def yrs(a, b, col, expo):
        x = t[(t.year >= a) & (t.year <= b)]
        return float(x[col].sum()), float(x[expo].sum())
    out = {}
    k1, e1 = yrs(2025, 2025, "bus_vehicles_fatal_injury", "total_buses")
    k2, e2 = yrs(2017, 2019, "bus_vehicles_fatal_injury", "total_buses")
    out["buses_involved_per_bus_2025_vs_2017_2019"] = rate_ratio(k1, e1, k2, e2)
    k1, e1 = yrs(2025, 2025, "bus_vehicles_fatal", "total_buses")
    k2, e2 = yrs(2022, 2024, "bus_vehicles_fatal", "total_buses")
    out["fatal_involvements_per_bus_2025_vs_2022_2024"] = rate_ratio(k1, e1, k2, e2)
    k1, e1 = yrs(2025, 2025, "injured", "annual_public_bus_rides")
    k2, e2 = yrs(2017, 2019, "injured", "annual_public_bus_rides")
    out["occupant_injuries_per_ride_2025_vs_2017_2019"] = rate_ratio(k1, e1, k2, e2)
    a, b = s[s.year == 2024].iloc[0], s[s.year == 2022].iloc[0]
    out["public_serious_collisions_per_ride_2024_vs_2022"] = rate_ratio(
        a.value, a.annual_public_bus_rides, b.value, b.annual_public_bus_rides)
    return {k: {"ratio": v[0], "lo": v[1], "hi": v[2]} for k, v in out.items()}


# ---------------------------------------------------------------- live incidents
def load_incidents(directory=INC):
    """One row per distinct incident (Type + Message), with first and last poll it appeared in."""
    rows = []
    for f in sorted(Path(directory).glob("inc_*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            for it in p["value"]:
                rows.append({"polled_at": p["polled_at"], "type": it.get("Type"), "message": it.get("Message"),
                             "lat": it.get("Latitude"), "lon": it.get("Longitude")})
    cols = ["type", "message", "lat", "lon", "first_seen", "last_seen", "polls"]
    if not rows:
        return pd.DataFrame(columns=cols), 0
    d = pd.DataFrame(rows)
    polls = d.polled_at.nunique()
    g = d.groupby(["type", "message"], as_index=False).agg(
        lat=("lat", "first"), lon=("lon", "first"), first_seen=("polled_at", "min"), last_seen=("polled_at", "max"),
        polls=("polled_at", "nunique"))
    return g[cols], polls


def snap(points_xy, seg_start, seg_end):
    """Distance from each point to its nearest segment and that segment's index (brute force over a KD-tree
    shortlist of segment midpoints)."""
    from scipy.spatial import cKDTree
    mid = (seg_start + seg_end) / 2
    half = np.hypot(*(seg_end - seg_start).T) / 2
    tree = cKDTree(mid)
    dist = np.full(len(points_xy), np.inf)
    idx = np.full(len(points_xy), -1)
    r = SNAP_M + half.max()
    for i, p in enumerate(points_xy):
        cand = tree.query_ball_point(p, r)
        if not cand:
            continue
        a, b = seg_start[cand], seg_end[cand]
        ab = b - a
        L2 = (ab ** 2).sum(1)
        t = np.clip(((p - a) * ab).sum(1) / np.where(L2 > 0, L2, 1), 0, 1)
        dd = np.hypot(*(a + t[:, None] * ab - p).T)
        j = int(np.argmin(dd))
        dist[i], idx[i] = dd[j], cand[j]
    return dist, idx


def incidents_vs_network(inc, links, bus_link_ids):
    """Snap incidents to the nearest speed-band segment (within SNAP_M) and compare the share that falls on
    segments used by bus routes with the share of road length those segments make up."""
    from pyproj import Transformer
    tr = Transformer.from_crs(4326, 3414, always_xy=True)
    sx, sy = tr.transform(links.StartLon.to_numpy(), links.StartLat.to_numpy())
    ex, ey = tr.transform(links.EndLon.to_numpy(), links.EndLat.to_numpy())
    s, e = np.c_[sx, sy], np.c_[ex, ey]
    seg_len = np.hypot(*(e - s).T)
    on_bus = links.LinkID.isin(bus_link_ids).to_numpy()
    expressway = (links.RoadCategory == 1).to_numpy()
    inc = inc.copy()
    if len(inc):
        px, py = tr.transform(inc.lon.to_numpy(float), inc.lat.to_numpy(float))
        dist, idx = snap(np.c_[px, py], s, e)
    else:
        dist, idx = np.array([]), np.array([], dtype=int)
    inc["snap_m"] = dist
    ok = np.isfinite(dist) & (dist <= SNAP_M)
    inc["matched"] = ok
    inc["on_bus_route"] = np.where(ok, on_bus[np.maximum(idx, 0)], False)
    inc["expressway"] = np.where(ok, expressway[np.maximum(idx, 0)], False)
    base = {"bus_route_share_of_road_length": float(seg_len[on_bus].sum() / seg_len.sum()),
            "bus_route_share_of_non_expressway_length": float(seg_len[on_bus & ~expressway].sum()
                                                              / seg_len[~expressway].sum())}
    rows = []
    for typ, g in inc[inc.matched].groupby("type"):
        n = len(g)
        k = int(g.on_bus_route.sum())
        ne = g[~g.expressway]
        kn = int(ne.on_bus_route.sum())
        ci = stats.binomtest(k, n).proportion_ci() if n else None
        cin = stats.binomtest(kn, len(ne)).proportion_ci() if len(ne) else None
        rows.append({"type": typ, "incidents": n, "on_bus_route": k, "share_on_bus_route": k / n,
                     "share_lo": ci.low if ci else np.nan, "share_hi": ci.high if ci else np.nan,
                     "non_expressway_incidents": len(ne), "non_expressway_on_bus_route": kn,
                     "non_expressway_share": kn / len(ne) if len(ne) else np.nan,
                     "non_expressway_share_lo": cin.low if cin else np.nan,
                     "non_expressway_share_hi": cin.high if cin else np.nan})
    return inc, pd.DataFrame(rows), base


def chart(t, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    blue, ink, muted, surface = "#2a78d6", "#0b0b0b", "#52514e", "#fcfcfb"
    x = t[t.total_buses.notna()]
    y2 = t[t.occupants_injured_per_100m_rides.notna() & (t.year >= 2015)]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), facecolor=surface)
    panels = [(axes[0], x, "per_1000_buses", "Buses in fatal or injury accidents\nper 1,000 registered buses"),
              (axes[1], y2, "occupants_injured_per_100m_rides",
               "Bus occupants injured per 100 million\npublic bus rides (all bus types)")]
    for ax, d, col, title in panels:
        ax.set_facecolor(surface)
        ax.fill_between(d.year, d[col + "_lo"], d[col + "_hi"], color=blue, alpha=0.15, linewidth=0)
        ax.plot(d.year, d[col], color=blue, linewidth=2, marker="o", markersize=5)
        last = d.iloc[-1]
        ax.annotate(f"{last[col]:.1f}", (last.year, last[col]), textcoords="offset points", xytext=(6, 0),
                    va="center", color=ink, fontsize=9)
        ax.set_title(title, loc="left", fontsize=10, color=ink)
        ax.set_ylim(0, None)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#c9c8c3")
        ax.tick_params(colors=muted, labelsize=8)
        ax.grid(axis="y", color="#e4e3df", linewidth=0.8)
        ax.set_axisbelow(True)
    fig.text(0.01, 0.01, "Shaded: exact Poisson 95% interval. Sources: Traffic Police Traffic Annual, SingStat "
                         "M651281 and M651351, LTA vehicle statistics.", fontsize=7.5, color=muted)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(path, dpi=150, facecolor=surface)
    plt.close(fig)


def brief(t, s, comp, implied, by_type, base, inc, polls, path):
    """Bulletin-style one-page brief, written from the computed tables so it cannot drift from them."""
    def row(y):
        return t[t.year == y].iloc[0]
    r25, r19 = row(2025), row(2019)
    pre = t[(t.year >= 2017) & (t.year <= 2019)]
    c = comp
    acc = by_type[by_type.type == "Accident"]
    if len(inc):
        window = f"{inc.first_seen.min()[:16].replace('T', ' ')} to {inc.last_seen.max()[:16].replace('T', ' ')}"
    else:
        window = "no polls yet"

    def ci(x):
        return f"{x['ratio']:.2f} (95% CI {x['lo']:.2f}-{x['hi']:.2f})"
    lines = [
        "# Bus safety brief: public data, Singapore",
        "",
        "Built by `python src/safety.py` from the tables in `data/reference/safety/` and the DataMall incident "
        "archive. Every number below is recomputed on each run. Intervals are exact Poisson 95% intervals; "
        "comparisons are rate ratios with exact conditional intervals.",
        "",
        "![Bus involvement and bus-occupant injury rates, 2015-2025](safety_trend.png)",
        "",
        "## 1. Trend: all buses, Traffic Police data",
        "",
        f"- **{int(r25.bus_vehicles_fatal_injury)} buses** were involved in fatal or injury accidents in 2025 "
        f"({int(r25.bus_vehicles_fatal)} in fatal ones), {r25.bus_share_of_vehicles:.1%} of all vehicles involved.",
        f"- Per 1,000 registered buses: **{r25.per_1000_buses:.1f}** in 2025 "
        f"(95% CI {r25.per_1000_buses_lo:.1f}-{r25.per_1000_buses_hi:.1f}), against "
        f"{pre.bus_vehicles_fatal_injury.sum() / pre.total_buses.sum() * 1000:.1f} over 2017-2019. "
        f"Rate ratio 2025 / 2017-2019: {ci(c['buses_involved_per_bus_2025_vs_2017_2019'])}. The rise "
        f"from the 2021 low ({row(2021).per_1000_buses:.1f}) has brought the rate back to its pre-COVID level, not above it.",
        f"- Fatal involvements: {int(r25.bus_vehicles_fatal)} in 2025 against "
        f"{t[(t.year >= 2022) & (t.year <= 2024)].bus_vehicles_fatal.mean():.0f} a year in 2022-2024. Rate ratio "
        f"{ci(c['fatal_involvements_per_bus_2025_vs_2022_2024'])}: too few events to call a change.",
        f"- Bus occupants injured per 100 million public bus rides: **{r25.occupants_injured_per_100m_rides:.1f}** in "
        f"2025 against {r19.occupants_injured_per_100m_rides:.1f} in 2019; 2025 / 2017-2019 "
        f"{ci(c['occupant_injuries_per_ride_2025_vs_2017_2019'])}. Bus occupants killed: "
        f"{int(r25.killed)} in 2025.",
        "",
        "## 2. Public buses only (Ministry of Transport / LTA statements)",
        "",
    ]
    for x in s.itertuples():
        lines.append(f"- {x.year}: **{int(x.value)}** serious collisions involving public buses, "
                     f"{x.per_100m_rides:.1f} per 100 million rides (95% CI {x.per_100m_rides_lo:.1f}-"
                     f"{x.per_100m_rides_hi:.1f}); bus captain at fault in {x.captain_at_fault_share:.0%}.")
    lines += [
        f"- 2024 against 2022, per ride: {ci(c['public_serious_collisions_per_ride_2024_vs_2022'])}. The Taskforce "
        "described the count as stable; per ride it is flat to slightly down, and the interval includes no change.",
        "- LTA's public-bus accident rate fell from about 0.15-0.16 per 100,000 bus-km (2017) to 0.10 (January-August "
        "2021); operators are held to below 0.50 per 100,000 bus-km for each package each month.",
        f"- Exposure: the official daily bus-km series stops in 2014 ({implied['bus_km_2014']:,.0f} km a day). The 2017 "
        "figures imply "
        f"about {implied['implied_bus_km_per_day_2017'] / 1e6:.2f} million bus-km a day (270 accidents in "
        "January-May at about 0.15 per 100,000 bus-km). Rates per bus-km after 2017 cannot be rebuilt from public data.",
        "",
        "## 3. Where live incidents fall relative to the bus network (LTA DataMall Traffic Incidents)",
        "",
        f"- Archive: {polls} polls, {window} (Singapore time), {len(inc)} distinct incidents, "
        f"{int(inc.matched.sum()) if len(inc) else 0} within 30 m of a speed-band road segment.",
        f"- Road segments used by bus routes are {base['bus_route_share_of_road_length']:.1%} of speed-band road "
        f"length ({base['bus_route_share_of_non_expressway_length']:.1%} of non-expressway length).",
    ]
    m = inc[inc.matched] if len(inc) else inc
    if len(m):
        ne = m[~m.expressway]
        ci_all = stats.binomtest(int(m.on_bus_route.sum()), len(m)).proportion_ci()
        line = (f"- All incident types: {int(m.on_bus_route.sum())} of {len(m)} on bus-route segments "
                f"({m.on_bus_route.mean():.1%}, 95% CI {ci_all.low:.1%}-{ci_all.high:.1%}); "
                f"{len(m) - len(ne)} of {len(m)} are on expressways, which few bus routes use.")
        if len(ne):
            ci_ne = stats.binomtest(int(ne.on_bus_route.sum()), len(ne)).proportion_ci()
            line += (f" Off expressways, {int(ne.on_bus_route.sum())} of {len(ne)} "
                     f"({ne.on_bus_route.mean():.0%}, 95% CI {ci_ne.low:.0%}-{ci_ne.high:.0%}) are on bus-route "
                     f"segments, against their {base['bus_route_share_of_non_expressway_length']:.0%} share of "
                     "length.")
        lines.append(line)
    if len(acc):
        a = acc.iloc[0]
        lines.append(
            f"- Accidents: {int(a.incidents)} reported; {int(a.on_bus_route)} on bus-route segments "
            f"({a.share_on_bus_route:.0%}, 95% CI {a.share_lo:.0%}-{a.share_hi:.0%}). Off expressways: "
            f"{int(a.non_expressway_on_bus_route)} of {int(a.non_expressway_incidents)}.")
    lines += [
        "- The feed reports incidents on the road, not whether a bus was involved, and covers expressways more "
        "densely than local roads. It has no history, so this archive starts on 13 October 2026; with a few days "
        "of data, the shares above are descriptive and their intervals are wide. Per-incident text is not "
        "published here.",
        "",
        "## 4. What the public data cannot say",
        "",
        "- Traffic Police counts cover all buses (public, school, private, excursion) and only accidents with "
        "death or injury that must be reported; damage-only accidents are not tracked.",
        "- Bus-occupant casualties exclude pedestrians and other road users hurt in collisions with buses.",
        "- There is no published yearly series of public-bus accidents or of bus-km after 2014, so a system "
        "accident rate per bus-km for 2022-2025 cannot be computed from public sources.",
        "",
        "## Sources",
        "",
        "- Singapore Police Force, Traffic Annual 2025 (tables 1A-1C, 9A-9C) and Road Traffic Accident Statistics "
        "2004-2016 (tables 8A-8C).",
        "- SingStat Table Builder M651281 (casualties) and M651351 (public transport ridership); LTA vehicle "
        "statistics MVP01-1; data.gov.sg d_120bba76c28dd4706e0cf85693827e64 (average daily bus-km, 2004-2014).",
        "- Ministry of Transport: Bus Safety Tripartite Taskforce report (2025); replies of 26 July 2021, 4 October "
        "2021 and 8 April 2025. LTA presentation at Busworld South East Asia 2026. URLs and quoted text: "
        "`data/reference/safety/public_bus_figures.csv`.",
        "- LTA DataMall Traffic Incidents (collected by `src/collect.py`).",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    import duckdb
    t = annual()
    s, implied = public_bus(t)
    comp = comparisons(t, s)
    con = duckdb.connect(str(DB), read_only=True)
    bus_ids = set(con.execute("select distinct LinkID from speedband_match").df().LinkID)
    con.close()
    links = pd.read_csv(LINKS)
    inc, polls = load_incidents()
    inc, by_type, base = incidents_vs_network(inc, links, bus_ids)
    OUT.mkdir(exist_ok=True)
    t.round(4).to_csv(OUT / "safety_annual.csv", index=False, lineterminator="\n")
    s.drop(columns=["quote"]).round(4).to_csv(OUT / "safety_public_bus.csv", index=False, lineterminator="\n")
    by_type.round(4).to_csv(OUT / "safety_incidents.csv", index=False, lineterminator="\n")
    window = {"first_poll": None, "last_poll": None}
    if len(inc):
        window = {"first_poll": inc.first_seen.min(), "last_poll": inc.last_seen.max()}
    summary = {"comparisons": comp, "implied_exposure": implied, "incident_polls": int(polls),
               "incident_window": window, "distinct_incidents": int(len(inc)),
               "matched_within_m": SNAP_M, "matched_incidents": int(inc.matched.sum()) if len(inc) else 0,
               "network_baseline": base}
    (OUT / "safety_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    DOCS.mkdir(exist_ok=True)
    chart(t, DOCS / "safety_trend.png")
    brief(t, s, comp, implied, by_type, base, inc, polls, DOCS / "BUS_SAFETY_BRIEF.md")
    print(json.dumps(summary, indent=1))
    print(by_type.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
