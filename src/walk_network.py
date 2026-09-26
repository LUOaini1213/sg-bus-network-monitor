"""Walking network from the BBBike OpenStreetMap extract of Singapore (download.bbbike.org, Singapore.osm.gz).

Streaming two-pass parse of the OSM XML: pass 1 keeps ways that people can walk on, pass 2 reads coordinates only for
the nodes those ways use. The way filter follows the osmnx 'walk' network definition: any highway except
motorways, cycleways, bus guideways, platforms, raceways and roads that are abandoned, planned, proposed or under
construction; ways tagged foot=no, access=private, service=private or area=yes are dropped.

Output: data/processed/walk_edges.parquet (u, v, length_m) and walk_nodes.parquet (node, x, y in SVY21).
The Overpass API was overloaded when this was built (HTTP 504 on small queries), hence the file extract.
"""
import gzip
import re
from pathlib import Path

import numpy as np
import pandas as pd
from lxml import etree
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "raw" / "Singapore.osm.gz"
OUT = ROOT / "data" / "processed"
EXCLUDE = re.compile(r"abandoned|bus_guideway|construction|cycleway|motor|no|planned|platform|proposed|raceway|razed")


def walkable(tags):
    h = tags.get("highway")
    if not h or EXCLUDE.search(h):
        return False
    if tags.get("area") == "yes" or tags.get("access") == "private" or tags.get("service") == "private":
        return False
    return tags.get("foot") != "no"


def pass_ways():
    edges = []
    with gzip.open(SRC, "rb") as f:
        for _, el in etree.iterparse(f, events=("end",), tag="way"):
            tags = {t.get("k"): t.get("v") for t in el.iterfind("tag")}
            if walkable(tags):
                refs = [int(n.get("ref")) for n in el.iterfind("nd")]
                edges.extend(zip(refs[:-1], refs[1:]))
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
    return np.array(edges, dtype=np.int64)


def pass_nodes(needed):
    ids, lat, lon = [], [], []
    with gzip.open(SRC, "rb") as f:
        for _, el in etree.iterparse(f, events=("end",), tag="node"):
            i = int(el.get("id"))
            if i in needed:
                ids.append(i); lat.append(float(el.get("lat"))); lon.append(float(el.get("lon")))
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
    return pd.DataFrame({"node": ids, "lat": lat, "lon": lon})


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    e = pass_ways()
    needed = set(np.unique(e).tolist())
    nodes = pass_nodes(needed)
    x, y = Transformer.from_crs(4326, 3414, always_xy=True).transform(nodes.lon.values, nodes.lat.values)
    nodes["x"], nodes["y"] = x, y
    pos = nodes.set_index("node")[["x", "y"]]
    ed = pd.DataFrame(e, columns=["u", "v"])
    ed = ed[ed.u.isin(pos.index) & ed.v.isin(pos.index)]
    pu, pv = pos.loc[ed.u].to_numpy(), pos.loc[ed.v].to_numpy()
    ed["length_m"] = np.hypot(pu[:, 0] - pv[:, 0], pu[:, 1] - pv[:, 1])
    ed.to_parquet(OUT / "walk_edges.parquet", index=False)
    nodes[["node", "x", "y"]].to_parquet(OUT / "walk_nodes.parquet", index=False)
    print(f"walkable segments {len(ed):,}; nodes {len(nodes):,}; total length {ed.length_m.sum() / 1000:,.0f} km")
