"""Build the static dashboard (docs/index.html, served by GitHub Pages) from the CSV outputs."""
import datetime as dt
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


def div(fig):
    return fig.to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False, "responsive": True})


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


def surveillance_map():
    fig = go.Figure()
    buttons, n = [], 0
    for i, base in enumerate(["2026-07", "2026-02"]):
        s = pd.read_csv(OUT / f"surveillance_2026-08_vs_{base}.csv", dtype={"stop": str})
        for flag, col in (("surge", BLUE), ("drop", RED)):
            d = s[s.flag == flag]
            fig.add_trace(go.Scattermapbox(
                lat=d.Latitude, lon=d.Longitude, mode="markers", name=f"{flag} vs {base}", visible=(i == 0),
                marker=dict(size=np.clip(np.sqrt(d[["2026-08", base]].max(axis=1)) / 3, 6, 22), color=col,
                            opacity=0.8),
                text=[f"{r.stop} {r.Description} ({r.RoadName})<br>{r[base]:,.0f} → {r['2026-08']:,.0f} "
                      f"boardings/weekday<br>{r['pct_change']:+.0%} ({r.vs_network:+.0%} vs network), z = {r.robust_z:.1f}"
                      for _, r in d.iterrows()], hoverinfo="text"))
            n += 1
    for i, base in enumerate(["2026-07", "2026-02"]):
        vis = [j // 2 == i for j in range(n)]
        buttons.append(dict(label=f"Aug 2026 vs {dt.date(int(base[:4]), int(base[5:]), 1):%b %Y}", method="update",
                            args=[{"visible": vis}]))
    layout(fig, 560, mapbox=MAP)
    fig.update_layout(updatemenus=[dict(buttons=buttons, x=0.99, xanchor="right", y=0.99, bgcolor="white")])
    return fig


def surveillance_table(base):
    s = pd.read_csv(OUT / f"surveillance_2026-08_vs_{base}.csv", dtype={"stop": str})
    s = s[s.flag != ""].head(12)
    rows = "".join(f"<tr><td>{r.stop}</td><td>{html.escape(str(r.Description))}</td><td>{html.escape(str(r.RoadName))}</td>"
                   f"<td class=n>{r[base]:,.0f}</td><td class=n>{r['2026-08']:,.0f}</td>"
                   f"<td class=n>{r['pct_change']:+.0%}</td><td class=n>{r.robust_z:.1f}</td></tr>" for _, r in s.iterrows())
    return (f"<table><thead><tr><th>Stop</th><th>Name</th><th>Road</th><th>{base}</th><th>2026-08</th>"
            f"<th>Change</th><th>z</th></tr></thead><tbody>{rows}</tbody></table>")


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
    p = pd.read_csv(OUT / "coverage_planning_area.csv")
    p = p[p.residents >= 20000].sort_values("resident_coverage").head(15).iloc[::-1]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=p.resident_coverage * 100, y=p.PLN_AREA_N.str.title(), orientation="h", name="housing land",
                         marker_color=BLUE, hovertemplate="%{y}: %{x:.1f}%<extra>residents on housing land</extra>"))
    fig.add_trace(go.Bar(x=p.resident_coverage_even * 100, y=p.PLN_AREA_N.str.title(), orientation="h",
                         name="even spread over subzone", marker_color=GREY,
                         hovertemplate="%{y}: %{x:.1f}%<extra>even spread</extra>"))
    fig.update_xaxes(title="% of residents within 400 m of a bus stop", range=[60, 100], gridcolor="#e4e7eb")
    layout(fig, 500, barmode="group")
    fig.update_layout(legend=dict(orientation="h", y=-0.12, x=0), margin=dict(l=10, r=10, t=10, b=40))
    return fig


def priority_section():
    f = OUT / "priority_screen.csv"
    if not f.exists() or len(pd.read_csv(f)) == 0:
        return ("<p class=note>Speed-band sampling runs every 15 minutes until the weekday AM peak and night "
                "reference periods are covered. This section fills in when <code>outputs/priority_screen.csv</code> "
                "exists.</p>")
    p = pd.read_csv(f, dtype={"from_stop": str, "to_stop": str}).head(40)
    fig = go.Figure(lines_trace(p, "screened links", RED, 5,
                                [f"{r.from_stop} → {r.to_stop}<br>{r.services} services, {r.AM_Peak_bph:.0f} buses/h"
                                 f"<br>AM peak {r.v_peak:.0f} km/h vs night {r.v_ref:.0f} km/h"
                                 f"<br>{r.bus_h_lost_per_h * 60:.1f} bus-min lost per hour" for r in p.itertuples()]))
    return div(layout(fig, 520, mapbox=MAP))


def quality_table():
    q = pd.read_csv(OUT / "data_quality.csv")
    rows = "".join(f"<tr><td>{html.escape(r.check)}</td><td>{r.table}</td><td class=n>{r.failing_rows:,}</td>"
                   f"<td class=n>{r.total_rows:,}</td><td class=n>{r.pass_rate:.2%}</td>"
                   f"<td>{'' if pd.isna(r.note) else html.escape(str(r.note))}</td></tr>" for r in q.itertuples())
    return (f"<table><thead><tr><th>Check</th><th>Table</th><th>Failing</th><th>Rows</th><th>Pass</th><th>Note</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>")


def kpis():
    c = pd.read_csv(OUT / "corridor_links.csv")
    pa = pd.read_csv(OUT / "coverage_planning_area.csv")
    p = pd.read_csv(OUT / "network_hourly_profile.csv")
    aug = p[(p.YEAR_MONTH == "2026-08") & (p.DAY_TYPE == "WEEKDAY")].tap_in_per_day.sum()
    cov = pa.residents_covered.sum() / pa.residents.sum()
    items = [(f"{len(c):,}", "directed stop-to-stop links"), (f"{(c.services >= 10).sum():,}", "links shared by 10+ services"),
             (f"{aug / 1e6:.2f} M", "bus boardings per weekday, Aug 2026"), (f"{cov:.1%}", "residents within 400 m of a stop")]
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
.note{{color:#52606d}} code{{background:#eef2f7;padding:1px 4px;border-radius:3px}}
footer{{margin-top:40px;color:#7b8794;font-size:12px}}
</style></head><body><main>
<h1>Singapore Bus Network Monitor</h1>
<p class=sub>Shared corridors, stop demand, walking coverage and data quality from LTA DataMall, URA and SingStat open data.
Built {built}. Code and method: <a href="https://github.com/LUOaini1213/sg-bus-network-monitor">GitHub</a>.</p>
<div class=kpis>{kpis}</div>

<h2>1. Where services share the road</h2>
<p class=note>Each line is a stop-to-stop link, coloured by scheduled buses per hour in the weekday AM peak
(sum over all services using the link, from the published headway bands).</p>
<div class=card>{corridor_map}</div>
<div class=card>{road_bar}</div>

<h2>2. When people board</h2>
<p class=note>Monthly totals divided by the number of weekdays or weekend/holiday days in each month
(MOM 2026 public holidays). June is the school holiday month.</p>
<div class=card>{profile}</div>

<h2>3. Stops that changed</h2>
<p class=note>Weekday boardings per day, August 2026 against a baseline month. A stop is flagged when its change is at
least 3.5 robust standard deviations from the network-wide shift and at least 25% relative to it, among stops with 300+
daily boardings.</p>
<div class=card>{surv_map}</div>
<div class="card wrap"><b>August vs July 2026</b>{surv_tab_jul}</div>
<div class="card wrap"><b>August vs February 2026</b>{surv_tab_feb}</div>

<h2>4. Who lives within 400 m of a stop</h2>
<p class=note>SingStat GHS 2025 residents placed on Master Plan 2019 housing parcels (weighted by plot ratio), then
intersected with 400 m straight-line catchments around every served stop.</p>
<div class=card>{cov_map}</div>
<div class=card>{cov_bar}</div>

<h2>5. Bus priority screening</h2>
<p class=note>Links shared by many buses where general traffic in the weekday AM peak is much slower than at night.
Screening numbers from LTA speed bands, used for ranking, not measured bus travel times.</p>
<div class=card>{priority}</div>

<h2>6. Data quality</h2>
<div class="card wrap">{quality}</div>

<footer>Data: LTA DataMall (Bus Routes, Bus Services, Bus Stops, Passenger Volume by Bus Stops, Traffic Speed Bands),
URA Master Plan 2019 (data.gov.sg), SingStat GHS 2025 table C020123, OpenStreetMap contributors (stop position cross-check).
Contains information from LTA DataMall, URA and SingStat used under their open data terms.</footer>
</main></body></html>"""

if __name__ == "__main__":
    DOCS.mkdir(exist_ok=True)
    page = PAGE.format(
        built=dt.date.today().isoformat(), kpis=kpis(), corridor_map=div(corridor_map()), road_bar=div(road_bar()),
        profile=div(profile()), surv_map=div(surveillance_map()), surv_tab_jul=surveillance_table("2026-07"),
        surv_tab_feb=surveillance_table("2026-02"), cov_map=div(coverage_map()), cov_bar=div(coverage_bar()),
        priority=priority_section(), quality=quality_table())
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    print(f"docs/index.html {len(page) / 1e6:.1f} MB")
