"""Screening states must not imply an active sampler or label old results with new dates."""
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import dashboard


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "OUT", tmp_path)
    return tmp_path


def result(out, rows):
    columns = ["from_stop", "to_stop", "services", "AM_Peak_bph", "v_peak", "v_ref", "bus_h_lost_per_h",
               "from_Latitude", "from_Longitude", "to_Latitude", "to_Longitude"]
    path = out / "priority_screen.csv"
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
    metadata = {"csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "min_snapshots": 4,
                "peak": {"n_snapshots": 4, "min_snapshot": "2026-09-28 07:30", "max_snapshot": "2026-09-28 08:30"},
                "night": {"n_snapshots": 4, "min_snapshot": "2026-09-27 22:00", "max_snapshot": "2026-09-27 23:00"}}
    (out / "priority_screen_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return path


def test_missing_result_does_not_claim_sampler_is_running(out):
    text = dashboard.priority_section()
    assert "No bus-priority screening result" in text
    assert "sampling runs" not in text


def test_empty_valid_result_is_distinct_from_missing_and_no_congestion(out):
    result(out, [])
    text = dashboard.priority_section()
    assert "2026-09-28 07:30" in text
    assert "No links meet the data requirements" in text
    assert "does not mean there is no congestion" in text
    assert "no snapshots" not in text


def test_metadata_for_a_different_result_does_not_supply_dates(out):
    path = result(out, [])
    path.write_bytes(path.read_bytes() + b"\n")
    text = dashboard.priority_section()
    assert "observation period is unavailable" in text
    assert "2026-09-28" not in text


def test_rendered_rank_table_preserves_stop_codes_and_excludes_zero_loss(out):
    result(out, [
        ["01012", "01013", 2, 60, 10, 25, 3.6, 1.3, 103.8, 1.301, 103.801],
        ["01014", "01015", 1, 10, 25, 20, 0, 1.31, 103.81, 1.311, 103.811],
    ])
    text = dashboard.priority_section()
    assert "Eligible links: 2. Positive estimated AM loss: 1." in text
    assert "<td>01012 → 01013</td>" in text
    assert "216.0</td>" in text
    assert "01014" not in text
    assert 'id="priority-map"' in text


def test_zero_loss_is_not_shown_as_missing_data(out):
    result(out, [["01012", "01013", 1, 10, 25, 20, 0, 1.3, 103.8, 1.301, 103.801]])
    text = dashboard.priority_section()
    assert "No eligible links have a positive AM loss" in text
    assert "No links meet the data requirements" not in text
