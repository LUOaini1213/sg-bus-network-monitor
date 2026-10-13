"""Bus finance and fare model: fare bands, cheapest service, transfer rebate, revenue, and workbook = Python."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import finance  # noqa: E402
import finance_model as fm  # noqa: E402


@pytest.mark.parametrize("km, band", [(0.4, 0), (3.2, 0), (3.3, 1), (4.2, 1), (4.3, 2), (40.2, 37), (40.3, 38),
                                      (95.0, 38)])
def test_fare_band_edges(km, band):
    assert finance.band_of(km) == band


def test_2026_day_counts_exclude_weekday_holidays():
    # 261 weekdays minus ten public holidays falling on weekdays.
    assert finance.annual_day_counts(2026) == {"WEEKDAY": 251, "WEEKENDS/HOLIDAY": 114}


def test_cheapest_service_decides_the_fare_class():
    trunk = np.arange(100, 139, dtype=float)
    express = trunk + 50
    assert finance.cheapest_class(5.0, 2.0, np.nan, trunk, 100.0, express) == ("feeder", 2.0)
    assert finance.cheapest_class(2.0, 2.5, np.nan, trunk, 100.0, express) == ("trunk", 2.0)  # tie: trunk
    assert finance.cheapest_class(np.nan, np.nan, 7.0, trunk, 100.0, express) == ("express", 7.0)
    assert finance.cheapest_class(9.0, np.nan, 3.0, trunk, 100.0, express) == ("trunk", 9.0)  # 106 < 150
    assert finance.cheapest_class(9.0, np.nan, 3.0, trunk, 100.0, express - 60) == ("express", 3.0)  # 90 < 106
    assert finance.cheapest_class(np.nan, np.nan, np.nan, trunk, 100.0, express)[0] is None


def test_scheduled_trips_by_period_and_after_midnight():
    bands = {"AM_Peak_Freq": "10", "AM_Offpeak_Freq": "15", "PM_Peak_Freq": "12", "PM_Offpeak_Freq": "20"}
    # 06:00-00:30: 30 min before 06:30 at 4/h, 2 h at 6/h, 8.5 h at 4/h, 2 h at 5/h, 5.5 h at 3/h.
    assert finance.scheduled_trips(360, 30, bands) == pytest.approx(2 + 12 + 34 + 10 + 16.5)
    bands["AM_Offpeak_Freq"] = "-"  # no off-peak service, and none before 06:30
    assert finance.scheduled_trips(360, 30, bands) == pytest.approx(12 + 10 + 16.5)
    assert finance.scheduled_trips(420, 1200, bands) == pytest.approx(1.5 * 6 + 2 * 5 + 1 * 3)  # 07:00-20:00


def fares_fixture():
    trunk = np.full(fm.N_BANDS, 200.0)
    trunk[0], trunk[1] = 100.0, 150.0
    f = {"trunk": trunk, "express": trunk + 50, "feeder": 100.0}
    half = {k: v / 2 for k, v in f.items()}
    return {"adult": f, "senior": half, "student": half, "workfare": f}


def test_transfer_rebate_by_hand():
    fares = fares_fixture()
    rides = np.zeros(fm.N_BANDS)
    rides[0], rides[1] = 3.0, 1.0  # P = 0.75, 0.25
    km = np.full(fm.N_BANDS, 50.0)
    km[0], km[1] = 1.0, 3.0
    # pairs: (1,1) -> 2 km band 0: 100+100-100 = 100; (1,3) and (3,1) -> 4 km band 1: 100+150-150 = 100;
    # (3,3) -> 6 km band 3: 150+150-200 = 100. Make (3,3) differ to keep the weights visible:
    fares["adult"]["trunk"][3] = 260.0  # 150 + 150 - 260 = 40
    expect = 0.75 * 0.75 * 100 + 2 * 0.75 * 0.25 * 100 + 0.25 * 0.25 * 40
    assert fm.transfer_rebate(rides, km, fares["adult"]["trunk"]) == pytest.approx(expect)


def test_revenue_blends_groups_and_applies_the_transfer_factor():
    fares = fares_fixture()
    rides = np.zeros((fm.N_BANDS, 3))
    rides[0, 0], rides[2, 0], rides[0, 1] = 1000.0, 1000.0, 500.0
    a = {"shares": {"adult": 0.5, "senior": 0.3, "student": 0.2, "workfare": 0.0}, "transfer_share": 0.2,
         "rep_km": np.r_[1.0, 3.7, 4.7, np.arange(5.7, 41.7)]}
    r = fm.revenue(rides, fares, a)
    blended = lambda c: 0.5 * c + 0.5 * c / 2  # noqa: E731  adult 0.5; senior + student 0.5 at half fare
    assert r["gross"] == pytest.approx((1000 * blended(100) + 1000 * blended(200) + 500 * blended(100)) / 100)
    assert r["adult_gross"] == pytest.approx((1000 * 100 + 1000 * 200 + 500 * 100) / 100)
    mean_adult = r["adult_gross"] * 100 / 2500
    assert r["transfer_factor"] == pytest.approx(1 - 0.2 * r["rebate_cents"] / mean_adult)
    assert r["revenue"] == pytest.approx(r["gross"] * r["transfer_factor"])


def test_flat_fare_is_revenue_neutral():
    rides = np.array([3.0, 1.0])
    adult = np.array([100.0, 200.0])
    f, more = fm.flat_fare(rides, adult)
    assert f == pytest.approx(125.0)
    assert (rides * f).sum() == pytest.approx((rides * adult).sum())
    assert more == pytest.approx(0.75)


def test_uniform_scenario_with_elasticity():
    rev, rides = fm.scenario_uniform(100.0, 1000.0, 0.10, -0.3)
    assert rides == pytest.approx(1000 * 1.1 ** -0.3)
    assert rev == pytest.approx(100 * 1.1 * 1.1 ** -0.3)


def test_projection_part_year_fare_reviews():
    fy25 = {"fare_revenue": 1000.0, "bus_and_bus_related_lease_income": 100.0, "other_operating_income": 10.0,
            "total_operating_expenditure": 2000.0}
    a = {"later_fre": 0.04, "ridership_growth": 0.0, "cost_growth": 0.0}
    rows = fm.projection(fy25, 1.02e9, 0.07, a, years=3)
    assert rows[1]["level"] == pytest.approx(1.02 * (1 + 95 / 365 * 0.07))
    assert rows[2]["level"] == pytest.approx(1.02 * 1.07 * (1 + 95 / 365 * 0.04))
    assert rows[3]["level"] == pytest.approx(1.02 * 1.07 * 1.04 * (1 + 95 / 365 * 0.04))
    assert rows[1]["deficit"] == pytest.approx(rows[1]["fare"] + 110 - 2000)


def test_fare_tables_match_the_published_first_band():
    cur = fm.load_fares(fm.CURRENT)
    new = fm.load_fares(fm.APPROVED)
    # PTC: adult card fare up to 3.2 km is $1.28 from 27 Dec 2025 and $1.40 from 26 Dec 2026.
    assert cur["adult"]["trunk"][0] == 128 and new["adult"]["trunk"][0] == 140
    # Concession tables stop at 'Over 7.2 km'; longer bands repeat that fare.
    assert cur["senior"]["trunk"][5] == cur["senior"]["trunk"][38]
    assert cur["adult"]["trunk"][38] > cur["adult"]["trunk"][5]


@pytest.fixture(scope="module")
def workbook(tmp_path_factory):
    xl_eval = pytest.importorskip("xl_eval")
    import finance_workbook
    path = tmp_path_factory.mktemp("wb") / "model.xlsx"
    finance_workbook.build(path=path)
    return xl_eval.Workbook(path)


@pytest.fixture(scope="module")
def python_model():
    t = pd.read_csv(ROOT / "outputs" / "finance_rides_by_band.csv")
    a = json.loads(fm.PARAMS.read_text(encoding="utf-8"))
    stats = json.loads((ROOT / "outputs" / "finance_inputs.json").read_text(encoding="utf-8"))
    return fm.model(t[["trunk", "feeder", "express"]].to_numpy(), t.rep_km.to_numpy(), fm.load_sources(), a, stats)


PAIRS = [("rides_total", "annual_rides"), ("adult_gross", "adult_gross"), ("gross_now", "gross"),
         ("rebate", "rebate_cents"), ("tfactor", "transfer_factor"), ("rev_now", "revenue"),
         ("rev_dec", "approved_revenue"), ("dec_change", "approved_change"),
         ("pub_adj", "published_fy2025_at_dec2025_fares"), ("rec_rev_ratio", "revenue_vs_published_adj"),
         ("rec_rides_ratio", "rides_ratio"), ("implied_yield", "implied_yield"), ("flat_fare", "flat_fare_cents"),
         ("flat_more", "flat_share_paying_more"), ("conc_delta", "concession_delta"),
         ("fee_per_bus_km", "fee_per_bus_km_floor"), ("fee_service_year", "fee_service_year"),
         ("spend_median_now", "spend_median_now"), ("spend_p90_dec", "spend_p90_dec"),
         ("deficit_fy2030", "deficit_fy2030"), ("fare_fy2030", "fare_fy2030"), ("fare_fy2026", "fare_fy2026"),
         ("rec_dec_ratio", "approved_increase_vs_ptc"), ("bus_share", "bus_share"),
         ("spend_p90_dec_income", "spend_p90_dec_income"), ("transfer_share", "transfer_share"),
         ("share_adult", ("shares", "adult")), ("share_free", ("shares", "free"))]


@pytest.mark.parametrize("cell, key", PAIRS)
def test_workbook_formulas_equal_the_python_model(workbook, python_model, cell, key):
    expected = python_model[key[0]][key[1]] if isinstance(key, tuple) else python_model[key]
    assert workbook.name(cell) == pytest.approx(expected, rel=1e-9, abs=1e-9)


def test_workbook_scenario_grid_equals_python(workbook, python_model):
    a = json.loads(fm.PARAMS.read_text(encoding="utf-8"))
    for i, c in enumerate(a["scenario_changes"]):
        for j, e in enumerate(a["scenario_elasticities"]):
            ref = f"Scenarios!{chr(ord('B') + j)}{3 + i}"
            assert workbook.cell(ref) == pytest.approx(python_model["scenarios"][(c, e)], rel=1e-9)


def test_derived_shares_by_hand():
    a = {"derivation": {"senior_bus_trips_2019": 180, "bus_rides_2019": 1000, "trips_2018": 6.0,
                        "journeys_2018": 4.5,
                        "card_shares": {"senior": 0.5, "pwd": 0.05, "student": 0.25, "workfare": 0.1, "child": 0.1}}}
    d = fm.derive(a)
    assert d["shares"]["senior"] == pytest.approx(0.18 * 1.1)
    assert d["shares"]["student"] == pytest.approx(0.09)
    assert d["shares"]["workfare"] == pytest.approx(0.036)
    assert d["shares"]["free"] == pytest.approx(0.036)
    assert sum(d["shares"].values()) == pytest.approx(1.0)
    assert d["transfer_share"] == pytest.approx(0.25)
