"""Missing speed observations must not become congestion or priority scores."""
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import priority  # noqa: E402


@pytest.fixture
def matched():
    return pd.DataFrame({
        "from_stop": ["01012", "01012"], "to_stop": ["01013", "01013"],
        "LinkID": [1, 2], "seg_m": [100.0, 300.0], "link_km": [0.4, 0.4],
    })


def snapshots(*speeds):
    return pd.DataFrame([
        {"LinkID": link, "kmh": speed, "snapshot": dt.datetime(2026, 9, 28, 8) + dt.timedelta(minutes=20 * i)}
        for i, pair in enumerate(speeds) for link, speed in enumerate(pair, start=1)
    ])


def test_link_speed_preserves_length_weighted_mean(matched):
    speed, count = priority.link_speed(matched, snapshots((25.0, 45.0)), min_snapshots=1)
    assert speed.loc[("01012", "01013")] == pytest.approx(40.0)
    assert count == 1


@pytest.mark.parametrize("missing", [np.nan, 0.0, np.inf, -np.inf, -5.0])
def test_link_speed_excludes_invalid_speed_and_its_weight(matched, missing):
    # The 300 m observation still covers at least half of the 400 m link.
    speed, count = priority.link_speed(matched, snapshots((missing, 25.0)), min_snapshots=1)
    assert speed.loc[("01012", "01013")] == pytest.approx(25.0)
    assert count == 1


@pytest.mark.parametrize("missing", [np.nan, 0.0, np.inf, -np.inf])
def test_all_invalid_speed_observations_remain_missing(matched, missing):
    speed, count = priority.link_speed(matched, snapshots((missing, missing)), min_snapshots=1)
    index = pd.MultiIndex.from_tuples([("01012", "01013")], names=["from_stop", "to_stop"])
    assert pd.isna(speed.reindex(index).iloc[0])
    # The existing sample-count diagnostic counts input snapshots, not valid rows or individual links.
    assert count == 1


def test_missing_snapshot_does_not_lower_the_median(matched):
    speed, count = priority.link_speed(
        matched, snapshots((20.0, 20.0), (np.nan, np.nan), (40.0, 40.0)), min_snapshots=1)
    assert speed.loc[("01012", "01013")] == pytest.approx(30.0)
    assert count == 3


def test_empty_or_unmatched_observations_have_no_speed(matched):
    empty = pd.DataFrame(columns=["LinkID", "kmh", "snapshot"])
    speed, count = priority.link_speed(matched, empty)
    assert speed.empty and count == 0
    unmatched = pd.DataFrame({"LinkID": [999], "kmh": [25.0], "snapshot": [dt.datetime(2026, 9, 28, 8)]})
    speed, count = priority.link_speed(matched, unmatched)
    assert speed.empty and count == 1


def test_each_link_needs_four_valid_snapshots(matched):
    matched.loc[1, ["from_stop", "to_stop"]] = ["01014", "01015"]
    matched["link_km"] = matched.seg_m / 1000
    observations = snapshots((25.0, 25.0), (25.0, np.nan), (25.0, 0.0), (25.0, np.inf))
    speed, count = priority.link_speed(matched, observations)
    assert count == 4
    assert speed.loc[("01012", "01013")] == pytest.approx(25.0)
    assert pd.isna(speed.loc[("01014", "01015")])


@pytest.mark.parametrize("n", [3, 4])
def test_sample_threshold_counts_snapshots_not_segments(matched, n):
    speed, count = priority.link_speed(matched, snapshots(*[(25.0, 45.0)] * n))
    assert count == n
    if n < 4:
        assert pd.isna(speed.loc[("01012", "01013")])
    else:
        assert speed.loc[("01012", "01013")] == pytest.approx(40.0)


def test_priority_screen_rejects_invalid_speeds_and_keeps_finite_rankings():
    rows = [
        {"from_stop": "slow", "v_peak": 10.0, "v_ref": 25.0},
        {"from_stop": "moderate", "v_peak": 20.0, "v_ref": 40.0},
        {"from_stop": "faster_than_night", "v_peak": 30.0, "v_ref": 20.0},
    ]
    for column in ("v_peak", "v_ref"):
        for invalid in (np.nan, 0.0, np.inf, -np.inf, -5.0):
            rows.append({"from_stop": "invalid", "v_peak": 10.0, "v_ref": 25.0, column: invalid})
    links = pd.DataFrame(rows).assign(match_ratio=0.5, AM_Peak_bph=60.0, link_km=1.0)
    below_coverage = links.iloc[[0]].assign(from_stop="unmatched", match_ratio=0.49)
    links = pd.concat([links, below_coverage], ignore_index=True)
    ranked = priority.screen_priority(links)
    assert ranked.from_stop.tolist() == ["slow", "moderate", "faster_than_night"]
    assert ranked.bus_h_lost_per_h.tolist() == pytest.approx([3.6, 1.5, 0.0])
    assert np.isfinite(ranked.bus_h_lost_per_h).all()
    assert "bus_h_lost_per_h" not in links  # screening does not overwrite the diagnostic input


@pytest.mark.parametrize("period", ["peak", "night"])
def test_static_match_does_not_hide_missing_observed_coverage(matched, period):
    matched["seg_m"] = [100.0, 900.0]
    matched["link_km"] = 1.0
    partial = snapshots(*[(10.0, np.nan)] * 4)
    complete = snapshots(*[(25.0, 25.0)] * 4)
    peak, _ = priority.link_speed(matched, partial if period == "peak" else complete)
    night, _ = priority.link_speed(matched, partial if period == "night" else complete)
    links = pd.DataFrame({"from_stop": ["01012"], "to_stop": ["01013"], "link_km": [1.0],
                          "match_ratio": [1.0], "AM_Peak_bph": [60.0]})
    links = links.join(peak.rename("v_peak"), on=["from_stop", "to_stop"])
    links = links.join(night.rename("v_ref"), on=["from_stop", "to_stop"])
    assert priority.screen_priority(links).empty


@pytest.mark.parametrize("covered_m, eligible", [(99.0, False), (100.0, True)])
def test_observed_coverage_threshold_uses_real_link_length(matched, covered_m, eligible):
    matched["seg_m"] = [covered_m, 200.0 - covered_m]
    matched["link_km"] = 0.2
    speed, _ = priority.link_speed(matched, snapshots(*[(25.0, np.nan)] * 4))
    if eligible:
        assert speed.loc[("01012", "01013")] == pytest.approx(25.0)
    else:
        assert pd.isna(speed.loc[("01012", "01013")])


def test_insufficiently_covered_snapshot_does_not_count_toward_four(matched):
    speed, count = priority.link_speed(matched, snapshots(*[(25.0, 45.0)] * 3, (25.0, np.nan)))
    assert count == 4
    assert pd.isna(speed.loc[("01012", "01013")])


@pytest.mark.parametrize("weight", [0.0, -50.0, np.nan, np.inf, -np.inf])
def test_invalid_segment_weights_do_not_affect_speed(matched, weight):
    matched["seg_m"] = [100.0, weight]
    matched["link_km"] = 0.1
    speed, _ = priority.link_speed(matched, snapshots(*[(25.0, 45.0)] * 4))
    assert speed.loc[("01012", "01013")] == pytest.approx(25.0)


def test_missing_link_length_cannot_bypass_observed_coverage(matched):
    with pytest.raises(ValueError, match="link_km"):
        priority.link_speed(matched.drop(columns="link_km"), snapshots(*[(25.0, 45.0)] * 4))


@pytest.mark.parametrize("length", [0.0, np.nan, np.inf, -0.4])
def test_invalid_link_length_remains_missing(matched, length):
    matched["link_km"] = length
    speed, _ = priority.link_speed(matched, snapshots(*[(25.0, 45.0)] * 4))
    assert speed.dropna().empty


def _corridor():
    return pd.DataFrame({"from_stop": ["01012"], "to_stop": ["01013"], "link_km": [0.4],
                         "match_ratio": [1.0], "AM_Peak_bph": [60.0]})


def test_match_carries_the_actual_corridor_length(monkeypatch):
    corridor = _corridor().assign(services=1, from_Latitude=1.3, from_Longitude=103.8,
                                 to_Latitude=1.302, to_Longitude=103.8)
    stops = pd.DataFrame({"BusStopCode": ["01012", "01013"], "RoadName": ["Test Rd", "Test Rd"]})
    segments = pd.DataFrame({"LinkID": [1], "RoadName": ["TEST ROAD"], "RoadCategory": [3],
                             "StartLat": [1.3001], "StartLon": [103.8], "EndLat": [1.3019], "EndLon": [103.8]})

    class Connection:
        def execute(self, query, params=None):
            self.frame = corridor if "corridor_links" in query else stops
            return self

        def df(self):
            return self.frame.copy()

    monkeypatch.setattr(priority.pd, "read_csv", lambda *args, **kwargs: segments.copy())
    matched, _ = priority.match(Connection())
    assert matched.LinkID.tolist() == [1]
    assert matched.link_km.tolist() == [0.4]
    assert matched.seg_m.iloc[0] < 400  # coverage uses route length, not the shorter matched chord


def test_publication_replaces_stale_result_when_samples_are_insufficient(matched, tmp_path):
    csv_path = tmp_path / "priority_screen.csv"
    csv_path.write_text("stale result", encoding="utf-8")
    (tmp_path / "priority_screen_metadata.json").write_text('{"eligible_links": 999}', encoding="utf-8")
    smoke_path = tmp_path / "priority_screen_smoke.csv"
    smoke_path.write_text("existing smoke result", encoding="utf-8")
    peak = snapshots(*[(10.0, 10.0)] * 3)
    night = snapshots(*[(25.0, 25.0)] * 4)
    ranked, metadata = priority.write_screen(matched, _corridor(), peak, night, output_dir=tmp_path)
    assert ranked.empty
    written = pd.read_csv(csv_path)
    assert written.empty and "bus_h_lost_per_h" in written
    assert metadata["eligible_links"] == 0 and metadata["min_snapshots"] == 4
    assert metadata["peak"] == {"min_snapshot": "2026-09-28T08:00:00", "max_snapshot": "2026-09-28T08:40:00",
                                 "n_snapshots": 3}
    assert metadata["night"]["n_snapshots"] == 4
    assert metadata["snapshot_timezone"] == "Asia/Singapore"
    assert metadata["csv_sha256"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert json.loads((tmp_path / "priority_screen_metadata.json").read_text()) == metadata
    assert smoke_path.read_text() == "existing smoke result"


def test_publication_without_observations_records_empty_windows(matched, tmp_path):
    empty = pd.DataFrame(columns=["LinkID", "kmh", "snapshot"])
    ranked, metadata = priority.write_screen(matched, _corridor(), empty, empty, output_dir=tmp_path)
    assert ranked.empty and pd.read_csv(tmp_path / "priority_screen.csv").empty
    assert metadata["peak"] == metadata["night"] == {
        "min_snapshot": None, "max_snapshot": None, "n_snapshots": 0,
    }
    assert metadata["eligible_links"] == 0


def test_smoke_publication_has_its_own_csv_and_metadata(matched, tmp_path):
    csv_path = tmp_path / "priority_screen.csv"
    metadata_path = tmp_path / "priority_screen_metadata.json"
    csv_path.write_text("formal result", encoding="utf-8")
    metadata_path.write_text("formal metadata", encoding="utf-8")
    ranked, metadata = priority.write_screen(
        matched, _corridor(), snapshots((10.0, 10.0)), snapshots((25.0, 25.0)),
        any_window=True, output_dir=tmp_path)
    assert len(ranked) == 1 and metadata["min_snapshots"] == 1
    smoke_path = tmp_path / "priority_screen_smoke.csv"
    assert metadata["csv_sha256"] == hashlib.sha256(smoke_path.read_bytes()).hexdigest()
    assert json.loads((tmp_path / "priority_screen_smoke_metadata.json").read_text()) == metadata
    assert csv_path.read_text() == "formal result"
    assert metadata_path.read_text() == "formal metadata"
