"""Build the static dashboard (docs/index.html, served by GitHub Pages) from the CSV outputs."""
import datetime as dt
import hashlib
import html
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
DOCS = ROOT / "docs"
CENTER = dict(lat=1.355, lon=103.82)
# Esri Light Gray Canvas raster tiles need no token (Carto basemaps now show an API-key watermark).
MAP = dict(style="white-bg", center=CENTER, zoom=10.2, layers=[dict(
    below="traces", sourcetype="raster", sourceattribution="Esri, HERE, Garmin, © OpenStreetMap contributors",
    source=["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"])])
FONT = dict(family="Inter, Segoe UI, Helvetica, Arial, sans-serif", size=12, color="#1f2933")
BLUE, ORANGE, RED, GREY = "#1f5fa8", "#e07b24", "#c0392b", "#9aa5b1"


def layout(fig, h=520, **kw):
    fig.update_layout(height=h, margin=dict(l=10, r=10, t=10, b=10), font=FONT, paper_bgcolor="white",
                      plot_bgcolor="white", legend=dict(orientation="h", y=1.02, x=0), **kw)
    return fig


def div(fig, chart_id):
    return fig.to_html(full_html=False, include_plotlyjs=False, div_id=chart_id,
                       config={"displaylogo": False, "responsive": True})


def lines_trace(df, name, color, width, hover):
    lat, lon, txt = [], [], []
    for r, h in zip(df.itertuples(), hover):
        lat += [r.from_Latitude, r.to_Latitude, None]
        lon += [r.from_Longitude, r.to_Longitude, None]
        txt += [h, h, None]
    return go.Scattermapbox(lat=lat, lon=lon, mode="lines", line=dict(color=color, width=width), name=name,
                            text=txt, hoverinfo="text")


def corridor_map():
    c = pd.read_csv(OUT / "corridor_links.csv", dtype={"from_stop": str, "to_stop": str}).dropna(
        subset=["from_Latitude", "to_Latitude"])
    c = c[c.link_km <= 3]
    bins = [(0, 20, "under 20 buses/h", "#c9d6e3", 1), (20, 40, "20-40", "#7fa7cf", 2),
            (40, 80, "40-80", ORANGE, 3), (80, 1e9, "80+", RED, 4.5)]
    fig = go.Figure()
    for lo, hi, name, col, w in bins:
        d = c[(c.AM_Peak_bph >= lo) & (c.AM_Peak_bph < hi)]
        hover = [f"{r.from_Description} → {r.to_Description}<br>{r.from_RoadName}<br>{r.services} services, "
                 f"{r.AM_Peak_bph:.0f} buses/h AM peak<br>{r.service_list[:80]}" for r in d.itertuples()]
        fig.add_trace(lines_trace(d, name, col, w, hover))
    return layout(fig, 560, mapbox=MAP)


def road_bar():
    r = pd.read_csv(OUT / "corridor_roads.csv").head(15).iloc[::-1]
    fig = go.Figure(go.Bar(x=r.km, y=r.road, orientation="h", marker_color=BLUE,
                           text=[f"{m:.0f} buses/h avg" for m in r.mean_am_peak_bph], textposition="outside",
                           cliponaxis=False,
                           hovertemplate="%{y}: %{x:.1f} km with 10+ services<extra></extra>"))
    fig.update_xaxes(title="km of road where 10+ services share every link", gridcolor="#e4e7eb",
                     range=[0, r.km.max() * 1.3])
    return layout(fig, 460)


def profile():
    p = pd.read_csv(OUT / "network_hourly_profile.csv")
    fig = go.Figure()
    colors = {"2026-02": GREY, "2026-06": "#7fa7cf", "2026-07": BLUE, "2026-08": RED}
    for (ym, t), d in p.groupby(["YEAR_MONTH", "DAY_TYPE"]):
        fig.add_trace(go.Scatter(x=d.hour, y=d.tap_in_per_day, mode="lines", name=f"{ym} {t.lower()}",
                                 line=dict(color=colors[ym], dash="solid" if t == "WEEKDAY" else "dot", width=2),
                                 hovertemplate="%{x}:00 — %{y:,.0f} boardings per day<extra>" + ym + "</extra>"))
    fig.update_xaxes(title="hour of day", dtick=2, gridcolor="#e4e7eb")
    fig.update_yaxes(title="bus boardings per day (all stops)", gridcolor="#e4e7eb")
    return layout(fig, 420)


CAT_COLORS = {"services withdrawn or rerouted away": RED, "services added or rerouted here": "#2e7d32",
              "service change (direction unclear)": "#8e44ad", "academic term": BLUE,
              "academic term (within 600 m of a campus)": "#7fa7cf",
              "school-term seasonality (June holidays in the baseline)": "#16a085",
              "sustained step up since July": ORANGE, "unexplained": "#52606d"}


def surveillance_map():
    e = pd.read_csv(OUT / "anomaly_evidence.csv", dtype={"stop": str})
    xy = pd.read_csv(OUT / "surveillance_2026-08_vs_baseline_median.csv", dtype={"stop": str})[["stop", "Latitude", "Longitude"]]
    e = e.merge(xy, on="stop")
    fig = go.Figure()
    for cat, d in e.groupby("category"):
        fig.add_trace(go.Scattermapbox(
            lat=d.Latitude, lon=d.Longitude, mode="markers", name=f"{cat} ({len(d)})",
            marker=dict(size=np.clip(np.sqrt(d[["2026-08", "baseline_median"]].max(axis=1)) / 3, 7, 22),
                        color=CAT_COLORS.get(cat, GREY), opacity=0.85),
            text=[f"{r.stop} {r.Description}<br>baseline {r.baseline_median:,.0f} -> Aug {r['2026-08']:,.0f} per weekday "
                  f"({r['pct_change']:+.0%})<br>Feb/Jun/Jul/Aug: {r['wd_2026-02']:,.0f} / {r['wd_2026-06']:,.0f} / "
                  f"{r['wd_2026-07']:,.0f} / {r['wd_2026-08']:,.0f}<br>{cat}"
                  + (f"<br>verified: {r.verified_event}" if isinstance(r.verified_event, str) else "")
                  for _, r in d.iterrows()], hoverinfo="text"))
    n = pd.read_csv(OUT / "new_stops_2026.csv", dtype={"stop": str})
    locs = pd.read_csv(OUT / "stop_profile_2026-08.csv", dtype={"stop": str})[["stop", "Latitude", "Longitude"]]
    nxy = n.merge(locs, on="stop")
    fig.add_trace(go.Scattermapbox(
        lat=nxy.Latitude, lon=nxy.Longitude, mode="markers", name=f"new stop ({len(nxy)})",
        marker=dict(size=12, color="black"),
        text=[f"{r.stop} {r.Description} (new; Jul {r['2026-07']:,.0f} -> Aug {r['2026-08']:,.0f})" for _, r in nxy.iterrows()],
        hoverinfo="text"))
    layout(fig, 600, mapbox=MAP)
    fig.update_layout(legend=dict(orientation="v", y=0.99, x=0.01, bgcolor="rgba(255,255,255,0.85)", font=dict(size=11)))
    return fig


def anomaly_table():
    e = pd.read_csv(OUT / "anomaly_evidence.csv", dtype={"stop": str})
    order = {c: i for i, c in enumerate(CAT_COLORS)}
    e = e.assign(o=e.category.map(order)).sort_values(["o", "robust_z"])

    def f(x):
        return "" if pd.isna(x) else f"{x:,.0f}"

    rows = "".join(
        f"<tr><td>{r.stop}</td><td>{html.escape(str(r.Description))}</td>"
        f"<td class=n>{f(r['wd_2026-02'])}</td><td class=n>{f(r['wd_2026-06'])}</td><td class=n>{f(r['wd_2026-07'])}</td>"
        f"<td class=n>{f(r['wd_2026-08'])}</td><td class=n>{r['pct_change']:+.0%}</td>"
        f"<td><span class=dot style='background:{CAT_COLORS.get(r.category, GREY)}'></span>{html.escape(r.category)}</td>"
        f"<td>{'' if pd.isna(r.verified_event) else html.escape(r.verified_event)}</td>"
        f"<td>{'No OD evidence' if pd.isna(r.od_biggest_changes) else html.escape(r.od_biggest_changes)}</td></tr>"
        for _, r in e.iterrows())
    return ("<table><thead><tr><th>Stop</th><th>Name</th><th>Feb</th><th>Jun</th><th>Jul</th><th>Aug</th>"
            "<th>vs baseline</th><th>Category</th><th>Verified event</th>"
            "<th>Largest destination changes (trips per weekday, Jul → Aug)</th></tr></thead><tbody>" + rows + "</tbody></table>")


def od_note():
    m = pd.read_csv(OUT / "od_planning_area_2026-08.csv")
    intra = m[m.origin_pa == m.dest_pa].trips_per_weekday.sum() / m.trips_per_weekday.sum()
    q = pd.read_csv(OUT / "od_quality.csv")
    return (f"{intra:.0%} of bus trips start and end in the same planning area, so buses mostly carry local and feeder "
            f"trips. OD totals match the stop tap-in totals to within {q.difference_pct.abs().max():.3f}%.")


def od_pairs():
    m = pd.read_csv(OUT / "od_planning_area_2026-08.csv")
    x = m[m.origin_pa != m.dest_pa].head(15).iloc[::-1]
    fig = go.Figure(go.Bar(x=x.trips_per_weekday, y=(x.origin_pa.str.title() + " -> " + x.dest_pa.str.title()),
                           orientation="h", marker_color=BLUE,
                           hovertemplate="%{y}: %{x:,.0f} trips per weekday<extra></extra>"))
    fig.update_xaxes(title="bus trips per weekday between planning areas, August 2026", gridcolor="#e4e7eb")
    return layout(fig, 480)


def od_lengths():
    h = pd.read_csv(OUT / "od_trip_length_2026-08.csv")
    h.columns = ["km", "trips"]
    labels = h.km.str.strip("[)").str.replace(", ", " to ") + " km"
    fig = go.Figure(go.Bar(x=labels, y=h.trips / h.trips.sum() * 100, marker_color=BLUE,
                           hovertemplate="%{x}: %{y:.1f}% of trips<extra></extra>"))
    fig.update_yaxes(title="% of weekday trips", gridcolor="#e4e7eb")
    fig.update_xaxes(title="straight-line distance between tap-in and tap-out stops")
    return layout(fig, 360)


def coverage_map():
    g = gpd.read_file(OUT / "coverage_subzone.geojson").to_crs(3414)
    g["geometry"] = g.simplify(15).to_crs(4326)
    g = g.to_crs(4326)
    g["id"] = g.index.astype(str)
    g["share"] = np.where(g.residents > 0, g.residents_covered / g.residents.replace(0, np.nan), np.nan)
    gj = json.loads(g[["id", "geometry"]].to_json())
    # Subzones under 1,000 residents (catchments, islands, industrial estates) are large but nearly empty and would
    # dominate the map; they are drawn in grey and still counted in every total.
    live = g[g.residents >= 1000]
    sparse = g[g.residents < 1000]
    fig = go.Figure(go.Choroplethmapbox(
        geojson=gj, locations=sparse.id, z=np.zeros(len(sparse)), colorscale=[[0, "#d9dde3"], [1, "#d9dde3"]],
        showscale=False, marker_opacity=0.5, marker_line_width=0.3, name="under 1,000 residents",
        text=[f"{r.SUBZONE_N}: {r.residents:,.0f} residents" for r in sparse.itertuples()], hoverinfo="text"))
    fig.add_trace(go.Choroplethmapbox(
        geojson=gj, locations=live.id, z=live.share * 100, zmin=60, zmax=100, colorscale="RdYlBu",
        marker_opacity=0.7, marker_line_width=0.4, colorbar=dict(title="% residents<br>within 400 m", thickness=12),
        text=[f"{r.SUBZONE_N} ({r.PLN_AREA_N})<br>{r.residents:,.0f} residents<br>{r.share:.1%} within 400 m of a stop"
              f"<br>{r.residents - r.residents_covered:,.0f} outside" for r in live.itertuples()], hoverinfo="text"))
    return layout(fig, 560, mapbox=MAP)


def coverage_bar():
    p = pd.read_csv(OUT / "coverage_walk_planning_area.csv")
    p = p[p.residents >= 20000].sort_values("coverage_walk").head(15).iloc[::-1]
    fig = go.Figure()
    for col, name, color in (("coverage_line", "straight line 400 m", GREY),
                             ("coverage_line13", "straight line 308 m (detour factor 1.3)", "#7fa7cf"),
                             ("coverage_walk", "OSM walking network 400 m", BLUE)):
        fig.add_trace(go.Bar(x=p[col] * 100, y=p.PLN_AREA_N.str.title(), orientation="h", name=name, marker_color=color,
                             hovertemplate="%{y}: %{x:.1f}%<extra>" + name + "</extra>"))
    fig.update_xaxes(title="% of residents within reach of a bus stop", range=[30, 100], gridcolor="#e4e7eb")
    layout(fig, 620, barmode="group")
    fig.update_layout(legend=dict(orientation="h", y=-0.1, x=0), margin=dict(l=10, r=10, t=10, b=40))
    return fig


def priority_section():
    f = OUT / "priority_screen.csv"
    if not f.exists():
        return "<p class=note>No bus-priority screening result has been built for this dashboard.</p>"
    p = pd.read_csv(f, dtype={"from_stop": str, "to_stop": str})
    # Tie dates to the actual result file, rather than showing stale metadata from another run.
    meta_file = OUT / "priority_screen_metadata.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
    if meta.get("csv_sha256") == hashlib.sha256(f.read_bytes()).hexdigest():
        def period(name):
            sample = meta[name]
            if not sample["n_snapshots"]:
                return "no snapshots"
            first = html.escape(sample["min_snapshot"].replace("T", " ")[:16])
            last = html.escape(sample["max_snapshot"].replace("T", " ")[:16])
            return f"{sample['n_snapshots']} snapshots, {first} to {last}"
        note = (f"<p class=note>Observed periods (Singapore time): weekday AM — {period('peak')}; "
                f"night — {period('night')}. Each eligible link needs at least {meta['min_snapshots']} "
                "valid snapshots in each period, each covering at least half its length.</p>")
    else:
        note = "<p class=note>The observation period is unavailable for this result file.</p>"
    if p.empty:
        return note + ("<p>No links meet the data requirements in this build. This does not mean there is no "
                       "congestion; more valid observations may be needed.</p>")
    positive = p[p.bus_h_lost_per_h > 0]
    note += (f"<p>Eligible links: {len(p):,}. Positive estimated AM loss: {len(positive):,}. "
             "The map shows up to 40 highest-ranked links; the table shows the first 10.</p>")
    if positive.empty:
        return note + "<p>No eligible links have a positive AM loss relative to the night reference.</p>"
    p = positive.head(40)
    fig = go.Figure(lines_trace(p, "screened links", RED, 5,
                                [f"{r.from_stop} → {r.to_stop}<br>{r.services} services, {r.AM_Peak_bph:.0f} buses/h"
                                 f"<br>AM peak {r.v_peak:.0f} km/h vs night {r.v_ref:.0f} km/h"
                                 f"<br>{r.bus_h_lost_per_h * 60:.1f} bus-min lost per hour" for r in p.itertuples()]))
    rows = "".join(
        f"<tr><td>{html.escape(r.from_stop)} → {html.escape(r.to_stop)}</td>"
        f"<td class=n>{r.services:.0f}</td><td class=n>{r.AM_Peak_bph:.1f}</td>"
        f"<td class=n>{r.v_peak:.1f}</td><td class=n>{r.v_ref:.1f}</td>"
        f"<td class=n>{r.bus_h_lost_per_h * 60:.1f}</td></tr>" for r in p.head(10).itertuples())
    table = ("<p class='table-hint note'>Scroll the table horizontally to read all columns.</p>"
             "<div class=wrap><table class=priority-table><caption>Highest estimated bus-priority opportunities</caption><thead><tr>"
             "<th>Stop link</th><th>Services</th><th>Scheduled buses/h</th><th>AM km/h</th><th>Night km/h</th>"
             "<th>Estimated bus-min lost/h</th></tr></thead><tbody>" + rows + "</tbody></table></div>")
    return note + div(layout(fig, 520, mapbox=MAP), "priority-map") + table


def quality_table():
    q = pd.read_csv(OUT / "data_quality.csv")
    rows = "".join(f"<tr><td>{html.escape(r.check)}</td><td>{r.table}</td><td class=n>{r.failing_rows:,}</td>"
                   f"<td class=n>{r.total_rows:,}</td><td class=n>{r.pass_rate:.2%}</td>"
                   f"<td>{'' if pd.isna(r.note) else html.escape(str(r.note))}</td></tr>" for r in q.itertuples())
    return (f"<table><thead><tr><th>Check</th><th>Table</th><th>Failing</th><th>Rows</th><th>Pass</th><th>Note</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>")


def kpis():
    c = pd.read_csv(OUT / "corridor_links.csv")
    cm = pd.read_csv(OUT / "coverage_measures.csv").set_index("measure").share
    p = pd.read_csv(OUT / "network_hourly_profile.csv")
    aug = p[(p.YEAR_MONTH == "2026-08") & (p.DAY_TYPE == "WEEKDAY")].tap_in_per_day.sum()
    lo, hi = cm.min(), cm.max()
    items = [(f"{len(c):,}", "directed stop-to-stop links"), (f"{(c.services >= 10).sum():,}", "links shared by 10+ services"),
             (f"{aug / 1e6:.2f} M", "bus boardings per weekday, Aug 2026"), (f"{lo:.0%} to {hi:.0%}", "residents within 400 m of a stop (walking network to straight line)")]
    return "".join(f"<div class=kpi><b>{v}</b><span>{t}</span></div>" for v, t in items)


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Singapore Bus Network Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"></script>
<style>
body{{margin:0;font:14px/1.55 Inter,Segoe UI,Helvetica,Arial,sans-serif;color:#1f2933;background:#f5f7fa}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px 60px}}
h1{{font-size:26px;margin:0 0 4px}} h2{{font-size:19px;margin:36px 0 6px}}
.sub{{color:#52606d;margin:0 0 18px}} .card{{background:#fff;border:1px solid #e4e7eb;border-radius:8px;padding:12px;margin:10px 0}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}}
.kpi{{background:#fff;border:1px solid #e4e7eb;border-radius:8px;padding:12px}}
.kpi b{{display:block;font-size:24px;color:#1f5fa8}} .kpi span{{color:#52606d}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{padding:5px 8px;border-bottom:1px solid #e4e7eb;text-align:left}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}} .wrap{{overflow-x:auto}}
.note{{color:#52606d}} .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}} code{{background:#eef2f7;padding:1px 4px;border-radius:3px}}
nav{{display:flex;flex-wrap:wrap;gap:8px 18px;margin:18px 0}} a{{color:#1f5fa8}} caption{{text-align:left;font-weight:600;padding:12px 0}}
.priority-table{{min-width:610px}} .priority-table td:first-child{{white-space:nowrap}} .table-hint{{display:none}}
@media(max-width:600px){{.table-hint{{display:block}}}}
footer{{margin-top:40px;color:#7b8794;font-size:12px}}
</style></head><body><main>
<h1>Singapore Bus Network Monitor</h1>
<p class=sub>Shared corridors, stop demand, walking coverage and data quality from LTA DataMall, URA and SingStat open data.
Built {built}. Code and method: <a href="https://github.com/LUOaini1213/sg-bus-network-monitor">GitHub</a>.</p>
<div class=kpis>{kpis}</div>
<nav aria-label="Dashboard sections"><a href="#corridors">Corridors</a><a href="#demand">Demand</a>
<a href="#anomalies">Changed stops</a><a href="#od">Trip destinations</a><a href="#coverage">Walking coverage</a>
<a href="#priority">Bus priority</a><a href="#quality">Data quality</a></nav>

<h2 id=corridors>1. Where services share the road</h2>
<p class=note>Each line is a stop-to-stop link, coloured by scheduled buses per hour in the weekday AM peak
(sum over all services using the link, from the published headway bands).</p>
<div class=card>{corridor_map}</div>
<div class=card>{road_bar}</div>

<h2 id=demand>2. When people board</h2>
<p class=note>Monthly totals divided by the number of weekdays or weekend/holiday days in each month
(MOM 2026 public holidays). June is the school holiday month.</p>
<div class=card>{profile}</div>

<h2 id=anomalies>3. Stops that changed</h2>
<p class=note>Weekday boardings per day in August 2026 against the median of February, June and July. A stop is flagged
when its change is at least 3.5 robust standard deviations from the network-wide shift and at least 25% relative to it,
among stops with 300+ daily boardings. Each flag is then categorised from the monthly series, the services at the stop
(DataMall routes, April vs September) and outside sources; only causes named by an official notice count as verified.
Details: <a href="https://github.com/LUOaini1213/sg-bus-network-monitor/blob/main/docs/ANOMALIES.md">ANOMALIES.md</a>.</p>
<div class=card>{surv_map}</div>
<div class="card wrap">{anomaly_table}</div>

<h2 id=od>4. Where trips go</h2>
<p class=note>Origin-destination pairs from the tap-in and tap-out stops of weekday bus trips (August 2026, per
weekday). {od_note}</p>
<div class=card>{od_pairs}</div>
<div class=card>{od_lengths}</div>

<h2 id=coverage>5. Who lives within 400 m of a stop</h2>
<p class=note>SingStat GHS 2025 residents placed on Master Plan 2019 housing parcels (weighted by plot ratio). Reach is
measured three ways: straight line (upper bound), straight line with a 1.3 detour factor, and along the OpenStreetMap
walking network (lower bound: OSM misses many void-deck and linkway shortcuts in HDB estates). The map shows the
straight-line measure. The low areas are the same under all three.</p>
<div class=card>{cov_map}</div>
<div class=card>{cov_bar}</div>

<h2 id=priority>6. Bus priority screening</h2>
<p class=note>Bus links where general traffic in the weekday AM peak is slower than at night.
Screening numbers from LTA speed bands, used for ranking, not measured bus travel times.</p>
<div class=card>{priority}</div>

<h2 id=quality>7. Data quality</h2>
<div class="card wrap">{quality}</div>

<footer>Data: LTA DataMall (Bus Routes, Bus Services, Bus Stops, Passenger Volume by Bus Stops, Traffic Speed Bands,
Passenger Volume by Origin Destination Bus Stops), URA Master Plan 2019 (data.gov.sg), SingStat GHS 2025 table C020123,
OpenStreetMap contributors (stop positions; walking network via the BBBike extract), official notices listed in data/reference.
Contains information from LTA DataMall, URA and SingStat used under their open data terms.</footer>
</main></body></html>"""

if __name__ == "__main__":
    DOCS.mkdir(exist_ok=True)
    page = PAGE.format(
        built=dt.date.today().isoformat(), kpis=kpis(), corridor_map=div(corridor_map(), "corridor-map"), road_bar=div(road_bar(), "road-bar"),
        profile=div(profile(), "demand-profile"), surv_map=div(surveillance_map(), "surveillance-map"), anomaly_table=anomaly_table(),
        od_pairs=div(od_pairs(), "od-pairs"), od_lengths=div(od_lengths(), "od-lengths"), od_note=od_note(),
        cov_map=div(coverage_map(), "coverage-map"), cov_bar=div(coverage_bar(), "coverage-bar"),
        priority=priority_section(), quality=quality_table())
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    print(f"docs/index.html {len(page) / 1e6:.1f} MB")
