"""run_all.py checks every raw input before running anything, and names what is missing."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_all  # noqa: E402


def test_a_clean_clone_lists_every_missing_raw_input(tmp_path):
    gone = run_all.missing_inputs(raw=tmp_path, skip_walk=False)
    assert set(gone) == {"build_db.py", "coverage.py", "walk_network.py", "od.py", "priority.py", "safety.py"}
    assert "datamall/transport_node_bus_202602*.csv" in gone["build_db.py"]


def test_present_inputs_are_not_listed_and_day_folders_count(tmp_path):
    dm = tmp_path / "datamall"
    (dm / "speedbands" / "2026-10-16").mkdir(parents=True)
    (dm / "speedbands" / "2026-10-16" / "sb_20261016_0830.csv.gz").write_bytes(b"")
    (dm / "speedband_links.csv").write_text("LinkID\n", encoding="utf-8")
    for m in ("202607", "202608"):
        (dm / f"origin_destination_bus_{m}.csv").write_text("", encoding="utf-8")
    gone = run_all.missing_inputs(raw=tmp_path, skip_walk=True)
    assert "priority.py" not in gone and "od.py" not in gone and "walk_network.py" not in gone
    assert gone["safety.py"] == ["datamall/incidents/inc_*.jsonl"]


def test_main_runs_nothing_and_exits_2_when_inputs_are_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run_all, "RAW", tmp_path)
    monkeypatch.setattr(run_all.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert run_all.main([]) == 2
    assert "What can be rebuilt" in capsys.readouterr().err


def test_datamall_fetches_the_months_the_analyses_use_and_rejects_unknown_arguments():
    sys.path.insert(0, str(ROOT / "src"))
    import pytest
    import datamall
    assert datamall.parse_args([]) == (["static", "pv", "speed"], None)
    assert datamall.parse_args(["pv", "od"]) == (["pv", "od"], None)
    assert "202602" in datamall.PV_MONTHS and set(datamall.OD_MONTHS) == {"202607", "202608"}
    assert datamall.parse_args(["od", "202608"]) == (["od"], ["202608"])
    with pytest.raises(SystemExit, match="unknown arguments"):
        datamall.parse_args(["pv", "2026-08"])
