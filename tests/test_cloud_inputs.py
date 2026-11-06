"""Priority-screen inputs rebuilt from outputs/corridor_links.csv and the data-collect branch."""
import gzip
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cloud_inputs  # noqa: E402


def test_corridor_tables_keep_stop_codes_and_take_each_stops_road(tmp_path):
    cl = pd.DataFrame({"from_stop": ["08057", "01012"], "to_stop": ["08069", "08057"], "services": [28, 3],
                       "AM_Peak_bph": [162.4, 20.0], "link_km": [0.5, 0.3], "from_RoadName": ["Orchard Rd", "Victoria St"],
                       "to_RoadName": ["Bras Basah Rd", "Orchard Rd"], "from_Latitude": [1.3, 1.29],
                       "from_Longitude": [103.8, 103.85], "to_Latitude": [1.29, 1.3], "to_Longitude": [103.85, 103.8]})
    cl.to_csv(tmp_path / "corridor_links.csv", index=False)
    links, stops = cloud_inputs.corridor_tables(tmp_path / "corridor_links.csv")
    assert links.columns.tolist() == cloud_inputs.LINK_COLS and links.from_stop.tolist() == ["08057", "01012"]
    assert stops.set_index("BusStopCode").RoadName.to_dict() == {
        "01012": "Victoria St", "08057": "Orchard Rd", "08069": "Bras Basah Rd"}


def test_main_links_the_day_folders_and_unpacks_the_geometry(tmp_path, monkeypatch):
    dc = tmp_path / "dc" / "data" / "raw" / "speedbands"
    (dc / "links").mkdir(parents=True)
    (dc / "links" / "speedband_links.csv.gz").write_bytes(gzip.compress(b"LinkID,RoadName\n1,A\n"))
    raw, db = tmp_path / "raw", tmp_path / "proc" / "sgbus.duckdb"
    monkeypatch.setattr(cloud_inputs, "RAW", raw)
    monkeypatch.setattr(cloud_inputs, "DB", db)
    cloud_inputs.main(tmp_path / "dc")
    assert (raw / "speedband_links.csv").read_text() == "LinkID,RoadName\n1,A\n"
    assert cloud_inputs.is_link(raw / "speedbands") and (raw / "speedbands").resolve() == dc.resolve()
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("select count(*) from corridor_links").fetchone()[0] == 7845
    con.close()
    cloud_inputs.main(tmp_path / "dc")   # a second run replaces the link
    assert cloud_inputs.is_link(raw / "speedbands")


@pytest.mark.skipif(os.name != "nt", reason="directory junctions exist only on Windows")
def test_without_the_symlink_privilege_windows_gets_a_junction_that_reads_the_same(tmp_path, monkeypatch):
    target = tmp_path / "dc" / "2026-10-16"
    target.mkdir(parents=True)
    (target / "sb_20261016_0830.csv.gz").write_bytes(b"x")

    def refused(*args, **kwargs):
        raise OSError(22, "A required privilege is not held by the client", None, 1314)

    monkeypatch.setattr(Path, "symlink_to", refused)
    link = tmp_path / "speedbands"
    cloud_inputs.link_dir(link, tmp_path / "dc")
    assert not link.is_symlink() and cloud_inputs.is_link(link)
    assert [p.name for p in link.rglob("sb_*.csv.gz")] == ["sb_20261016_0830.csv.gz"]
    link.unlink()   # removes the junction, not the files behind it
    assert (target / "sb_20261016_0830.csv.gz").exists() and not link.exists()


def test_a_symlink_refusal_is_not_hidden_outside_windows(tmp_path, monkeypatch):
    def refused(*args, **kwargs):
        raise OSError(1, "refused")

    monkeypatch.setattr(Path, "symlink_to", refused)
    monkeypatch.setattr(cloud_inputs, "WINDOWS", False)
    with pytest.raises(OSError):
        cloud_inputs.link_dir(tmp_path / "link", tmp_path)
