"""The headline numbers in README.md, recomputed from the committed outputs.

Each check formats a value from outputs/ the way the README prints it and requires that exact text in the README,
so a rerun that changes an output, or a hand edit of a README number, fails here until the two agree again.
Nothing here needs raw data or a DataMall key.
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
sys.path.insert(0, str(ROOT / "src"))


def readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def plain():
    """The README without bold markers, for table rows and sentences that bolden one figure."""
    return readme().replace("**", "")


def has(text, source=None):
    source = plain() if source is None else source
    assert text in source, f"README.md does not say: {text!r}"


def j(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def n(x):
    return f"{x:,}"


def pct(x, nd=1):
    return f"{100 * x:.{nd}f}%"


def sec(x):
    return f"{x:.0f} s"


def signed_sec(x):
    return f"{'+' if round(x) >= 0 else '−'}{abs(x):.0f} s"


# ---------------------------------------------------------------- Bus Arrival

def test_bus_arrival_sample_and_arrivals():
    s, q = j("busarrival_summary.json"), j("busarrival_quality.json")
    days = s["days"]
    has(f"over {len(days)} weekday mornings, ETA error")
    has(f"({len(days)} weekday mornings, 16 October to 6 November 2026)")
    assert days[0] == "2026-10-16" and days[-1] == "2026-11-06"
    has(f"{n(q['answers'])} stop answers")
    a = s["arrivals"]
    has(f"{n(a['zero'] + a['gone'])} inferred: {n(a['zero'])} from a monitored ETA crossing zero and "
        f"{n(a['gone'])} from a bus leaving the list. {n(a['lost'])} buses were dropped far from the stop, and "
        f"{n(a['gap'])} were still listed")
    has(f"{n(sum(r['n'] for r in s['eta']))} predictions.")
    first = {m["date"]: m["first_answer"][11:16] for m in s["mornings"]}
    earliest, latest = min(first, key=first.get), max(first, key=first.get)
    assert (earliest, latest) == ("2026-10-26", "2026-11-06")
    has(f"ranges from {first[earliest]} (26 October) to {first[latest]} (6 November)")


def test_bus_arrival_eta_table_and_headline():
    s = j("busarrival_summary.json")
    rows = {r["horizon"]: r for r in s["eta"]}
    for h, label in [("0-2", "0–2"), ("2-5", "2–5"), ("5-10", "5–10"), ("10-20", "10–20")]:
        r = rows[h]
        has(f"| {label} min | {n(r['n'])} | {sec(r['mae_s'])} | {signed_sec(r['bias_s'])} | {pct(r['within_60'])} | "
            f"{pct(r['within_120'])} | {sec(r['p90_abs_s'])} |")
    has(f"ETA error grows from {sec(rows['0-2']['mae_s'])} (MAE, 0–2 min ahead) to {sec(rows['10-20']['mae_s'])} "
        f"(10–20 min)")
    has(f"and {pct(s['bunched_share'])} of headways are bunched")


def test_bus_arrival_monitored_corridors_and_rules():
    s = j("busarrival_summary.json")
    mon = {(r["monitored"], r["horizon"]): r["mae_s"] for r in s["eta_by_monitored"]}
    has(f"an MAE of {sec(mon[(0, '10-20')])} at 10–20 minutes against {sec(mon[(1, '10-20')])} for monitored buses, "
        f"and {sec(mon[(0, '0-2')])} against {sec(mon[(1, '0-2')])} at 0–2 minutes")
    q = j("busarrival_quality.json")
    has(f"{pct(1 - q['monitored_share'])} of listed buses were not monitored")
    cor = {(r["corridor"], r["horizon"]): r["mae_s"] for r in s["eta_by_corridor"]}
    long = sorted(((cor[(c, "10-20")], c) for c in ("Marine Parade Rd", "Orchard Rd", "Telok Blangah Rd")), reverse=True)
    has(f"At 10–20 minutes {long[0][1]} is the worst (MAE {sec(long[0][0])}), ahead of {long[1][1]} "
        f"({sec(long[1][0])}) and {long[2][1]} ({sec(long[2][0])})")
    spread = max(max(cor[(c, h)] for c in ("Marine Parade Rd", "Orchard Rd", "Telok Blangah Rd"))
                 - min(cor[(c, h)] for c in ("Marine Parade Rd", "Orchard Rd", "Telok Blangah Rd"))
                 for h in ("0-2", "2-5", "5-10"))
    assert spread < 4
    has("within 4 s of each other up to 10 minutes")
    rule = {(r["rule"], r["horizon"]): r["bias_s"] for r in s["eta_by_rule"]}
    zero = [rule[("zero", h)] for h in ("0-2", "2-5", "5-10")]
    has(f"up to 10 minutes out ({signed_sec(min(zero)).replace(' s', '')} to {signed_sec(max(zero))})")
    has(f"grows to {signed_sec(rule[('zero', '10-20')])} at 10–20 minutes")
    gone = [rule[("gone", h)] for h in ("0-2", "2-5", "5-10", "10-20")]
    has(f"carry {signed_sec(min(gone)).replace(' s', '')} to {signed_sec(max(gone))} of bias")


def test_bus_arrival_headways_bunching_and_quality():
    s, q = j("busarrival_summary.json"), j("busarrival_quality.json")
    cv = s["median_cv_by_corridor"]
    has(f"{n(s['headways'])} observed headways. {n(s['stop_services_with_cv'])} service-stop pairs have 4 or more. "
        f"Their median coefficient of variation is {s['median_cv']:.2f}: {cv['Marine Parade Rd']:.2f} on Marine Parade Rd, "
        f"{cv['Orchard Rd']:.2f} on Orchard Rd and {cv['Telok Blangah Rd']:.2f} on Telok Blangah Rd.")
    per = s["bunched_by_period"]
    has(f"{pct(s['bunched_share'])} of headways were shorter than a quarter of the scheduled headway for the period: "
        f"{pct(per['am_peak']['bunched_share'])} of the {n(per['am_peak']['headways'])} AM-peak headways and "
        f"{pct(per['am_offpeak']['bunched_share'])} of the {n(per['am_offpeak']['headways'])} off-peak ones.")
    has(f"{n(per['am_peak']['headways'])} of the {n(s['headways'])} headways are in the AM peak")
    b = pd.read_csv(OUT / "busarrival_bunching.csv").set_index("corridor")
    has(f"By corridor: {pct(b.bunched_share['Marine Parade Rd'])} on Marine Parade Rd, {pct(b.bunched_share['Orchard Rd'])} "
        f"on Orchard Rd and {pct(b.bunched_share['Telok Blangah Rd'])} on Telok Blangah Rd.")
    has(f"Against the median observed headway instead, the share is {pct(s['bunched_share_median_ref'])}.")
    has(f"{n(q['blank_answers'])} of the {n(q['answers'])} answers ({pct(q['blank_answers'] / q['answers'])})")
    has(f"its first ETA was empty in {pct(q['nextbus_missing_eta_share'], 2)} of cases")
    load, kind = q["load_share"], q["type_share"]
    has(f"{pct(load['SEA'])} \"seats available\" (SEA), {pct(load['SDA'])} \"standing available\" (SDA) and "
        f"{pct(load['LSD'], 2)} \"limited standing\" (LSD)")
    has(f"{pct(kind['SD'])} single-deck, {pct(kind['DD'])} double-deck, {pct(kind['BD'], 2)} bendy")


def test_no_headway_spans_more_than_the_gap_limit_of_missing_answers():
    import busarrival_analysis as an
    s = j("busarrival_summary.json")
    assert 0 < s["max_answer_gap_in_headway_s"] <= an.MAX_GAP_S
    has(f"The longest stretch without an answer inside any headway is {sec(s['max_answer_gap_in_headway_s'])}")


# ---------------------------------------------------------------- bus priority

def test_priority_screen_counts_and_agreement():
    meta, stab = j("priority_screen_metadata.json"), j("priority_stability.json")
    scr = pd.read_csv(OUT / "priority_screen.csv", dtype={"from_stop": str, "to_stop": str})
    k = len(meta["mornings"])
    assert stab["summary"]["mornings"] == k == 14
    assert meta["eligible_links"] == len(scr)
    has(f"over {k} weekday mornings the ranking of {n(len(scr))} links is broadly stable "
        f"(median Kendall's tau {stab['summary']['median_pairwise_kendall_tau']:.2f})")
    has(f"(median overlap {stab['summary']['median_pairwise_top20_overlap']:.2f})")
    has(f"{n(len(scr))}; {n(int((scr.bus_h_lost_per_h > 0).sum()))} have a positive estimated loss on the combined "
        f"screen, which totals {scr.bus_h_lost_per_h.sum():.1f} bus-hours lost per hour")
    pairs = pd.DataFrame(stab["pairs"])
    has(f"Over the {len(pairs)} pairs of mornings, Kendall's tau is {pairs.kendall_tau.min():.2f}–"
        f"{pairs.kendall_tau.max():.2f} (median {pairs.kendall_tau.median():.2f}) and the top-20 overlap is "
        f"{pairs.top20_overlap.min():.2f}–{pairs.top20_overlap.max():.2f} (median {pairs.top20_overlap.median():.2f})")
    counts = {m["date"]: m["n_snapshots"] for m in meta["mornings"]}
    assert (counts["2026-10-16"], counts["2026-11-05"], counts["2026-11-04"], counts["2026-10-19"]) == (4, 4, 5, 6)
    others = [v for d, v in counts.items() if d not in ("2026-10-16", "2026-11-05", "2026-11-04", "2026-10-19")]
    has(f"the other {len(others)} mornings {min(others)} to {max(others)}")


def test_priority_night_reference_and_top20():
    meta = j("priority_screen_metadata.json")
    night = meta["night"]
    hours = night["by_hour"]
    assert not {"22", "23"} & set(hours) and hours.get("00") == 1 and max(hours) == "05"
    has(f"{night['n_snapshots']} snapshots, all between 00:")
    has("only one is before 01:00")
    scr = pd.read_csv(OUT / "priority_screen.csv", dtype={"from_stop": str, "to_stop": str})
    top = scr.head(20).mornings_in_daily_top20
    k = len(meta["mornings"])
    has(f"{int((top >= 7).sum())} of the 20 are in the daily top 20 on at least 7 of the {k} mornings, and "
        f"{int((top >= 10).sum())} on at least 10. One is on all {k}")
    assert int((top == k).sum()) == 1
    assert tuple(scr.head(20)[top == k].iloc[0][["from_stop", "to_stop"]]) == ("72019", "83119")  # Jln Eunos
    by = pd.read_csv(OUT / "priority_by_morning.csv", dtype={"from_stop": str, "to_stop": str})
    tot = by.merge(scr[["from_stop", "to_stop"]]).groupby("morning").bus_h_lost_per_h.sum()
    assert (tot.idxmin(), tot.idxmax()) == ("2026-10-23", "2026-11-04")
    has(f"runs from {tot.min():.1f} (23 October) to {tot.max():.1f} (4 November) across the {k} mornings")


def test_dashboard_priority_note_matches_the_committed_screen():
    meta = j("priority_screen_metadata.json")
    page = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    scr = pd.read_csv(OUT / "priority_screen.csv")
    assert f"{len(meta['mornings'])} weekday mornings ({', '.join(m['date'] for m in meta['mornings'])})" in page
    assert f"night — {meta['night']['n_snapshots']} snapshots" in page
    assert f"Positive estimated AM loss: {n(int((scr.bus_h_lost_per_h > 0).sum()))}." in page


# ---------------------------------------------------------------- forecasts

def forecast_rows(csv):
    m = pd.read_csv(OUT / csv).set_index(["model", "horizon_min"])
    names = {"persistence": "Persistence", "historical_average": "Historical average", "lightgbm": "LightGBM",
             "gnn_mean_of_seeds": "Graph neural net, mean of 5 seeds"}
    out = []
    for key, label in names.items():
        cells = [f"{m.mae[(key, h)]:.2f} ({m.mae_lo[(key, h)]:.2f}–{m.mae_hi[(key, h)]:.2f})" for h in (20, 40, 60)]
        out.append(f"| {label} | {' | '.join(cells)} | {m.rmse[(key, 20)]:.2f} |")
    return out


@pytest.mark.parametrize("csv", ["forecast_metrics.csv", "forecast_cv_metrics.csv"])
def test_forecast_tables(csv):
    for row in forecast_rows(csv):
        has(row)


# ---------------------------------------------------------------- the other sections

def test_demand_coverage_od_anomalies_and_safety():
    p = pd.read_csv(OUT / "network_hourly_profile.csv")
    wd = p[p.DAY_TYPE == "WEEKDAY"].groupby("YEAR_MONTH").tap_in_per_day.sum()
    per_day = wd["2026-08"] / wd["2026-07"] - 1
    raw = 20 * wd["2026-08"] / (23 * wd["2026-07"]) - 1   # 23 weekdays in July 2026, 20 in August (National Day)
    has(f"raw totals show weekday boardings falling {-100 * raw:.1f}% from July to August 2026; per weekday they "
        f"rose {100 * per_day:.1f}%")
    has(f"rose {100 * per_day:.1f}%, from {wd['2026-07'] / 1e6:.2f} M to {wd['2026-08'] / 1e6:.2f} M")
    c = pd.read_csv(OUT / "coverage_measures.csv").set_index("measure").share
    walk, line, detour = c["OSM walking network 400 m"], c["straight line 400 m"], c["straight line 308 m (detour factor 1.3)"]
    has(f"{100 * walk:.1f}–{100 * line:.1f}% of residents live within 400 m of a stop")
    has(f"| Straight line, 400 m (upper bound) | {pct(line)} |")
    has(f"| Straight line, 308 m (400 m with a 1.3 detour factor) | {pct(detour)} |")
    has(f"| Along the OpenStreetMap walking network, 400 m (lower bound) | {pct(walk)} |")
    od = pd.read_csv(OUT / "od_quality.csv")
    assert od.difference_pct.abs().max() < 0.0145
    has("to within 0.014% in both months")
    a = pd.read_csv(OUT / "anomaly_evidence.csv", dtype={"stop": str})
    flags = a.flag.value_counts()
    has(f"{len(a)} stops are flagged: {flags['surge']} surges and {flags['drop']} drops")
    academic = int(a.category.str.startswith("academic term").sum())
    has(f"{academic} surges match the university and ITE terms")
    has(f"{int((a.category == 'unexplained').sum())} stops have no verified cause")
    s = j("safety_summary.json")["comparisons"]
    b = s["buses_involved_per_bus_2025_vs_2017_2019"]
    has(f"rate ratio of {b['ratio']:.2f} ({b['lo']:.2f}–{b['hi']:.2f})")
    f = s["fatal_involvements_per_bus_2025_vs_2022_2024"]
    has(f"rate ratio {f['ratio']:.2f} ({f['lo']:.2f}–{f['hi']:.2f})")
    has(f"{j('safety_summary.json')['distinct_incidents']} distinct incidents were reported")


def test_mutant_count():
    sys.path.insert(0, str(ROOT / "tests"))
    import mutate
    has(f"which makes {len(mutate.MUTANTS)} deliberate bugs")


def test_the_readme_makes_no_one_command_rebuild_claim():
    """The demand, OD, anomaly and coverage steps need archived raw files; the README must not say otherwise."""
    assert not re.search(r"rebuilt from raw files by one command", readme(), re.I)
    has("## What can be rebuilt", readme())
