"""Offline OD evidence regressions using invented surveillance and monthly trips."""
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import anomalies  # noqa: E402
import od  # noqa: E402


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    out.mkdir()
    # 00001 and 00005 are abnormal only against the multi-month median.
    # 00003 is flagged against both sources, with different flag metadata.
    flags = {"2026-07": [("", 0.01), ("drop", -0.6), ("drop", -0.3), ("", 0.02), ("", 0.01)],
             "baseline_median": [("surge", 0.75), ("", 0.05), ("surge", 0.5), ("", 0.01), ("drop", -0.7)]}
    for baseline, pairs in flags.items():
        rows = [{"stop": f"{i:05d}", "Description": f"Stop {i}", "RoadName": "Fixture Rd",
                 "flag": flag, "pct_change": change, "robust_z": change * 10,
                 "baseline_median": 1000.0, "2026-08": 1000 * (1 + change)}
                for i, (flag, change) in enumerate(pairs, 1)]
        pd.DataFrame(rows).to_csv(out / f"surveillance_2026-08_vs_{baseline}.csv", index=False)
    db = tmp_path / "fixture.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table stop_pa (stop varchar, pa varchar)")
    con.executemany("insert into stop_pa values (?, ?)",
                    [(f"9000{i}", f"DEST {name}") for i, name in enumerate(("ALPHA", "BETA", "GAMMA", "DELTA"), 1)])
    daily_trips = [("00001", "90001", 10, 50), ("00001", "90002", 20, 10),
                   ("00001", "90003", 5, 25), ("00001", "90004", 1, 2),
                   ("00002", "90001", 100, 10), ("00003", "90002", 20, 50),
                   ("00004", "90001", 10, 100), ("00005", "90004", 40, 2)]
    # July has 23 weekdays, August 20: OD changes remain July -> August,
    # regardless of which surveillance baseline selected the stop.
    for month, days, value_idx in (("202607", 23, 2), ("202608", 20, 3)):
        con.execute(f'create table od_{month} (o varchar, d varchar, "hour" integer, DAY_TYPE varchar, trips bigint)')
        con.executemany(f"insert into od_{month} values (?, ?, ?, ?, ?)",
                        [(row[0], row[1], 8, "WEEKDAY", row[value_idx] * days) for row in daily_trips])
    monkeypatch.setattr(od, "OUT", out)
    yield con, out, db, tmp_path
    con.close()


def test_median_only_flags_retain_od_evidence_before_deduplication(evidence):
    con, *_ = evidence
    result = od.flagged_stop_flows(con)
    assert set(result.stop) == {"00001", "00002", "00003", "00005"}
    median_only = result[result.stop == "00001"].set_index("dest_pa")
    assert set(median_only.index) == {"DEST ALPHA", "DEST BETA", "DEST GAMMA"}
    assert median_only.loc["DEST ALPHA", ["trips_jul", "trips_aug", "change"]].tolist() == [10, 50, 40]
    assert set(median_only.flag) == {"surge"} and set(median_only["pct_change"]) == {0.75}
    assert set(median_only.flag_baseline) == {"baseline_median"}
    assert result.loc[result.stop == "00005", "flag_baseline"].tolist() == ["baseline_median"]
    assert not result.duplicated(["stop", "dest_pa"]).any()


def test_july_and_dual_flags_keep_a_single_explained_source(evidence):
    con, *_ = evidence
    result = od.flagged_stop_flows(con)
    for stop, change in (("00002", -0.6), ("00003", -0.3)):
        row = result[result.stop == stop].iloc[0]
        assert row.flag == "drop" and row["pct_change"] == change
        assert row.flag_baseline == "2026-07"
    # A large OD movement alone does not introduce an unflagged stop.
    assert "00004" not in set(result.stop)


def test_no_surveillance_flags_does_not_invent_od_candidates(evidence):
    con, out, *_ = evidence
    for path in out.glob("surveillance_*.csv"):
        source = pd.read_csv(path, dtype={"stop": str})
        source["flag"] = ""
        source.to_csv(path, index=False)
    result = od.flagged_stop_flows(con)
    assert result.empty
    assert {"stop", "dest_pa", "trips_jul", "trips_aug", "change", "flag_baseline"} <= set(result.columns)


def test_anomaly_evidence_receives_median_only_stops_without_duplicate_destinations(evidence, monkeypatch):
    con, out, db, root = evidence
    od.flagged_stop_flows(con).to_csv(out / "od_flagged_stops.csv", index=False)
    con.execute("create table stops (BusStopCode varchar, Latitude double, Longitude double, Description varchar, RoadName varchar)")
    con.executemany("insert into stops values (?, ?, ?, ?, ?)",
                    [(f"{i:05d}", 1.30, 103.80, f"Stop {i}", "Fixture Rd") for i in range(1, 6)])
    raw = root / "data" / "raw" / "datamall"
    raw.mkdir(parents=True)
    (raw / "BusRoutes_20260926_fixture.json").write_text("[]", encoding="utf-8")
    reference = root / "data" / "reference"
    reference.mkdir()
    pd.DataFrame(columns=["event_id", "date_from", "stops"]).to_csv(reference / "network_events_2026.csv", index=False)
    volume = pd.DataFrame([{"stop": f"{i:05d}", "YEAR_MONTH": month, "DAY_TYPE": "WEEKDAY", "tap_in_per_day": 1000.0}
                           for i in range(1, 6) for month in ("2026-02", "2026-06", "2026-07", "2026-08")])
    for name, value in (("ROOT", root), ("RAW", raw), ("DB", db), ("OUT", out)):
        monkeypatch.setattr(anomalies, name, value)
    monkeypatch.setattr(anomalies, "daily", lambda _con: volume)
    monkeypatch.setattr(anomalies, "routes", lambda _path: pd.Series(dtype=object))
    result = anomalies.main().set_index("stop")
    assert set(result.index) == {"00001", "00003", "00005"}
    assert result.loc["00001", "od_biggest_changes"] == "Dest Alpha 10->50; Dest Gamma 5->25"
    assert result.loc["00005", "od_biggest_changes"] == "Dest Delta 40->2"
    assert result.loc["00003", "od_biggest_changes"] == "Dest Beta 20->50"
    # The anomaly report still uses its own multi-month flag; the OD metadata
    # does not overwrite it with the July-only selection for the same stop.
    assert result.loc["00003", "flag"] == "surge"
