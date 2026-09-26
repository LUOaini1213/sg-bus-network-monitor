"""Bus priority screening: shared bus corridors where general traffic is slow in the peak.

1. Match LTA speed-band road segments to bus links. A bus link is drawn as the straight chord between its two stops
   (SVY21, EPSG:3414). A segment is matched when its midpoint lies within BUFFER_M of the chord and it points the same
   way (bearing within MAX_ANGLE degrees), so the opposite carriageway is excluded.
2. Speed per snapshot: length-weighted mean of the band midpoints of the matched segments.
3. Peak speed = median over weekday AM peak snapshots (07:30-09:30); reference speed = median over night snapshots
   (22:00-05:00). Scheduled bus-hours lost per hour = AM peak buses/h x km x (1/v_peak - 1/v_ref).

Speed bands describe general traffic. Buses also dwell at stops, so these are screening numbers for ranking
corridors, not measured bus travel times.
"""
import datetime as dt
import gzip
import re
from pathlib import Path

import duckdb
import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "datamall"
DB = ROOT / "data" / "processed" / "sgbus.duckdb"
OUT = ROOT / "outputs"
BUFFER_M, MAX_ANGLE, MAX_LINK_KM = 25, 40, 2.0
AM = (dt.time(7, 30), dt.time(9, 30))
NIGHT = (dt.time(22, 0), dt.time(5, 0))


ABBR = {"RD": "ROAD", "AVE": "AVENUE", "ST": "STREET", "DR": "DRIVE", "CRES": "CRESCENT", "BT": "BUKIT",
        "UPP": "UPPER", "LOR": "LORONG", "NTH": "NORTH", "STH": "SOUTH", "CTRL": "CENTRAL", "PK": "PARK",
        "BLVD": "BOULEVARD", "CL": "CLOSE", "JLN": "JALAN", "TG": "TANJONG", "C'WEALTH": "COMMONWEALTH",
        "S'GOON": "SERANGOON", "HWY": "HIGHWAY", "PL": "PLACE", "TER": "TERRACE", "GDNS": "GARDENS", "KG": "KAMPONG",
        "CTR": "CENTRE", "INDL": "INDUSTRIAL", "E": "EAST", "W": "WEST", "INTL": "INTERNATIONAL", "SG": "SUNGEI",
        "SVC": "SERVICE", "GDN": "GARDEN", "HTS": "HEIGHTS", "IND": "INDUSTRIAL", "AYE": "AYER RAJAH EXPRESSWAY", "PIE": "PAN ISLAND EXPRESSWAY", "CTE": "CENTRAL EXPRESSWAY",
        "TPE": "TAMPINES EXPRESSWAY", "SLE": "SELETAR EXPRESSWAY", "BKE": "BUKIT TIMAH EXPRESSWAY",
        "KJE": "KRANJI EXPRESSWAY", "ECP": "EAST COAST PARKWAY", "KPE": "KALLANG PAYA LEBAR EXPRESSWAY",
        "MCE": "MARINA COASTAL EXPRESSWAY"}


def norm_road(name):
    words = re.sub(r"[^A-Z0-9' ]", "", str(name).upper()).split()
    return " ".join(ABBR.get(w, w) for w in words)


def bearing(x0, y0, x1, y1):
    return np.degrees(np.arctan2(x1 - x0, y1 - y0)) % 360


def match(con):
    cl = con.execute("""select from_stop, to_stop, services, AM_Peak_bph, link_km,
                               from_Latitude, from_Longitude, to_Latitude, to_Longitude from corridor_links
                        where link_km > 0 and link_km <= ?""", [MAX_LINK_KM]).df()
    chords = gpd.GeoDataFrame(cl, geometry=[LineString([(a, b), (c, d)]) for a, b, c, d in
                                            zip(cl.from_Longitude, cl.from_Latitude, cl.to_Longitude, cl.to_Latitude)],
                              crs=4326).to_crs(3414)
    xy = chords.geometry.apply(lambda g: g.coords[:]).tolist()
    chords["bearing"] = [bearing(p[0][0], p[0][1], p[1][0], p[1][1]) for p in xy]

    sb = pd.read_csv(RAW / "speedband_links.csv")
    seg = gpd.GeoDataFrame(sb, geometry=[LineString([(a, b), (c, d)]) for a, b, c, d in
                                         zip(sb.StartLon, sb.StartLat, sb.EndLon, sb.EndLat)], crs=4326).to_crs(3414)
    seg["seg_m"] = seg.length
    c = seg.geometry.apply(lambda g: g.coords[:]).tolist()
    seg["bearing"] = [bearing(p[0][0], p[0][1], p[1][0], p[1][1]) for p in c]
    seg = seg[seg.seg_m > 5]
    mid = gpd.GeoDataFrame(seg[["LinkID", "RoadName", "RoadCategory", "seg_m", "bearing"]],
                           geometry=seg.geometry.interpolate(0.5, normalized=True), crs=3414)
    buf = chords[["from_stop", "to_stop", "bearing", "geometry"]].copy()
    buf["geometry"] = buf.buffer(BUFFER_M, cap_style=2)
    j = gpd.sjoin(mid, buf, predicate="within", how="inner", lsuffix="seg", rsuffix="bus")
    diff = (j.bearing_seg - j.bearing_bus).abs() % 360
    j = j[np.minimum(diff, 360 - diff) <= MAX_ANGLE]
    m = j[["from_stop", "to_stop", "LinkID", "RoadName", "RoadCategory", "seg_m"]].copy()
    # Parallel roads (an expressway beside its service road, a viaduct over an arterial) fall inside the same buffer.
    # Keep segments named like either stop's road; if a link has none, drop expressways, viaducts and tunnels.
    roads = con.execute("select BusStopCode, RoadName from stops").df().set_index("BusStopCode").RoadName.map(norm_road)
    m["seg_road"] = m.RoadName.map(norm_road)
    m["same_road"] = (m.seg_road == m.from_stop.map(roads)) | (m.seg_road == m.to_stop.map(roads))
    has_same = m.groupby(["from_stop", "to_stop"]).same_road.transform("any")
    grade_sep = (m.RoadCategory == 1) | m.seg_road.str.contains("VIADUCT|TUNNEL|EXPRESSWAY|PARKWAY")
    m = m[np.where(has_same, m.same_road, ~grade_sep)]
    cover = m.groupby(["from_stop", "to_stop"]).seg_m.sum().rename("matched_m")
    cl = cl.join(cover, on=["from_stop", "to_stop"])
    cl["match_ratio"] = (cl.matched_m / (cl.link_km * 1000)).clip(upper=1)
    return m, cl


def load_snapshots(window, weekday_only):
    frames = []
    for f in sorted((RAW / "speedbands").glob("sb_*.csv.gz")):
        t = dt.datetime.strptime(f.stem.split(".")[0], "sb_%Y%m%d_%H%M")
        lo, hi = window
        in_win = (lo <= t.time() <= hi) if lo < hi else (t.time() >= lo or t.time() <= hi)
        if not in_win or (weekday_only and t.weekday() >= 5):
            continue
        with gzip.open(f, "rt") as z:
            d = pd.read_csv(z)
        # Band 8 has no upper bound; use its minimum + 5 km/h like the others' midpoint offset.
        d["kmh"] = np.where(d.SpeedBand >= 8, d.MinimumSpeed + 5, (d.MinimumSpeed + d.MaximumSpeed + 1) / 2)
        d["snapshot"] = t
        frames.append(d[["LinkID", "kmh", "snapshot"]])
    return pd.concat(frames) if frames else pd.DataFrame(columns=["LinkID", "kmh", "snapshot"])


def link_speed(m, snaps):
    if snaps.empty:
        return pd.Series(dtype=float, index=pd.MultiIndex.from_tuples([], names=["from_stop", "to_stop"])), 0
    x = m.merge(snaps, on="LinkID")
    x["wv"] = x.kmh * x.seg_m
    g = x.groupby(["from_stop", "to_stop", "snapshot"])[["wv", "seg_m"]].sum()
    per = g.wv / g.seg_m
    return per.groupby(level=[0, 1]).median(), snaps.snapshot.nunique()


if __name__ == "__main__":
    import sys
    con = duckdb.connect(str(DB))
    m, cl = match(con)
    print(f"{len(cl):,} bus links (<= {MAX_LINK_KM} km); {cl.matched_m.notna().sum():,} matched to speed-band segments; "
          f"median coverage {cl.match_ratio.median():.0%}")
    any_window = "--any" in sys.argv  # smoke test on whatever snapshots exist
    peak, n_peak = link_speed(m, load_snapshots((dt.time(0, 0), dt.time(23, 59)) if any_window else AM, not any_window))
    ref, n_ref = link_speed(m, load_snapshots(NIGHT, False))
    cl = cl.join(peak.rename("v_peak"), on=["from_stop", "to_stop"]).join(ref.rename("v_ref"), on=["from_stop", "to_stop"])
    print(f"peak snapshots: {n_peak}, night snapshots: {n_ref}")
    if not any_window and (n_peak < 4 or n_ref < 4):
        print("not enough weekday AM peak or night snapshots yet (need 4 of each); priority_screen.csv not written")
        con.execute("create or replace table speedband_match as select * from m")
        sys.exit(0)
    ok = cl[(cl.match_ratio >= 0.5) & cl.v_peak.notna() & cl.v_ref.notna()].copy()
    ok["bus_h_lost_per_h"] = ok.AM_Peak_bph * ok.link_km * (1 / ok.v_peak - 1 / ok.v_ref).clip(lower=0)
    ok = ok.sort_values("bus_h_lost_per_h", ascending=False)
    OUT.mkdir(exist_ok=True)
    ok.to_csv(OUT / ("priority_screen_smoke.csv" if any_window else "priority_screen.csv"), index=False)
    con.execute("create or replace table speedband_match as select * from m")
    print(ok.head(15)[["from_stop", "to_stop", "services", "AM_Peak_bph", "link_km", "v_peak", "v_ref",
                       "bus_h_lost_per_h"]].round(2).to_string(index=False))
