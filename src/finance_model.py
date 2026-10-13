"""Bus finance and fare model: an Excel workbook with live formulas (openpyxl) and the same model in Python.

Revenue side. Annual bus rides by fare class (trunk / feeder / express) and distance band come from the DataMall
origin-destination data (finance.py). Each band is priced with the PTC card fare tables for each fare group
(adult, senior citizen / persons with disabilities, student, Workfare Transport Concession), weighted by the
fare-group shares in Assumptions. The distance fare is charged per journey, so rides that are a transfer leg
earn less than a separate fare: the transfer factor removes, per transfer ride, the average difference
F(a) + F(b) - F(a + b) over pairs of rides drawn from the observed adult trunk ride distribution.

Reconciliation. The estimate is compared with the fare revenue in LTA's audited Bus Contracting accounts, and
annual rides with LTA's published average daily bus ridership.

Cost side. Tendered package awards (LTA estimated total fee over the contract term) give fees per service-year;
LTA's audited service fees and scheduled bus-km give a system cost per bus-km.

Scenarios. Uniform fare changes with a price elasticity; the approved December 2026 fare tables; a concession
change; a revenue-neutral flat fare; monthly spend of a regular commuter against the adult monthly pass.

The Python functions here compute every headline cell of the workbook from the same inputs; the test suite
evaluates the workbook formulas (pycel) and requires them to match.
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "data" / "reference" / "finance_sources.json"
PARAMS = ROOT / "data" / "reference" / "finance_assumptions.json"
OUT = ROOT / "outputs"
N_BANDS = 39
GROUPS = ["adult", "senior", "student", "workfare"]
TABLE_NAMES = {"adult": "Adult", "senior": "Senior Citizen/ Persons with Disabilities", "student": "Student",
               "workfare": "Workfare Transport Concession Scheme"}
CLASSES = ["trunk", "feeder", "express"]
CURRENT, APPROVED = "card_from_2025_12_27", "card_from_2026_12_26_approved"


def derive(a):
    """Fare-group shares and the transfer share from the published counts in a["derivation"]."""
    d = a["derivation"]
    senior = d["senior_bus_trips_2019"] / d["bus_rides_2019"]
    c = d["card_shares"]
    shares = {"senior": senior * (c["senior"] + c["pwd"]) / c["senior"], "student": senior * c["student"] / c["senior"],
              "workfare": senior * c["workfare"] / c["senior"], "free": senior * c["child"] / c["senior"]}
    shares["adult"] = 1 - shares["senior"] - shares["student"] - shares["workfare"] - shares["free"]
    return dict(a, shares=shares, transfer_share=1 - d["journeys_2018"] / d["trips_2018"])


def load_sources(path=REF):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_fares(column=CURRENT, sources=None):
    """{group: {"trunk": 39 cents, "express": 39 cents, "feeder": cents}} with concession tables expanded by their cap
    (bands past the last published band pay the last band's fare)."""
    src = sources or load_sources()
    tables = {t["table"]: t["rows"] for t in src["fares"]["tables"]}
    out = {}
    for g, name in TABLE_NAMES.items():
        out[g] = {}
        for cls, label in (("trunk", "Trunk"), ("express", "Express"), ("feeder", "Feeder")):
            rows = tables[f"{name} Fare Structure ({label} Services)"]
            vals = [float(r[column] if r.get(column) is not None else r[CURRENT]) for r in rows]
            if cls == "feeder":
                out[g][cls] = vals[0]
            else:
                out[g][cls] = np.array([vals[min(i, len(vals) - 1)] for i in range(N_BANDS)])
    return out


def band_fares(fares, group):
    """(39 x 3) cents per ride: trunk, feeder (band 0 only), express."""
    f = fares[group]
    return np.c_[f["trunk"], np.full(N_BANDS, f["feeder"]), f["express"]]


def blended(fares, shares):
    return sum(shares[g] * band_fares(fares, g) for g in GROUPS)


def transfer_rebate(trunk_rides, rep_km, adult_trunk):
    """Expected F(a) + F(b) - F(a + b), cents, for two rides drawn independently from the trunk ride distribution."""
    p = trunk_rides / trunk_rides.sum()
    km = rep_km[:, None] + rep_km[None, :]
    comb = np.clip(np.ceil(np.round(km - 3.2, 1) - 1e-9), 0, N_BANDS - 1).astype(int)
    r = adult_trunk[:, None] + adult_trunk[None, :] - adult_trunk[comb]
    return float((p[:, None] * p[None, :] * r).sum())


def revenue(rides, fares, a):
    """Annual fare revenue in S$ and the pieces the workbook shows."""
    gross = float((rides * blended(fares, a["shares"])).sum()) / 100
    adult_gross = float((rides * band_fares(fares, "adult")).sum()) / 100
    mean_adult = adult_gross * 100 / rides.sum()
    rebate = transfer_rebate(rides[:, 0], a["rep_km"], fares["adult"]["trunk"])
    factor = 1 - a["transfer_share"] * rebate / mean_adult
    return {"rides": float(rides.sum()), "adult_gross": adult_gross, "gross": gross, "mean_adult_cents": mean_adult,
            "rebate_cents": rebate, "transfer_factor": factor, "revenue": gross * factor}


def scenario_uniform(base_revenue, base_rides, change, elasticity):
    q = (1 + change) ** elasticity
    return base_revenue * (1 + change) * q, base_rides * q


def flat_fare(rides, adult):
    """Revenue-neutral flat adult fare (cents) and the share of rides that would pay more than now."""
    f = float((rides * adult).sum() / rides.sum())
    more = float((rides * (adult < f)).sum() / rides.sum())
    return f, more


def monthly_spend(fare_cents, rides_per_month):
    return fare_cents * rides_per_month / 100


def concession_revenue(rides, fares, a, change):
    """Revenue with senior / PwD fares scaled by (1 + change), same transfer factor as the base case."""
    f = {g: dict(v) for g, v in fares.items()}
    f["senior"] = {k: v * (1 + change) for k, v in fares["senior"].items()}
    base = revenue(rides, fares, a)
    gross = float((rides * blended(f, a["shares"])).sum()) / 100
    return gross * base["transfer_factor"]


def projection(fy25, pub_adj, dec_change, a, years=5):
    """FY2025/26 actual, then five years: fare level, ridership index, fare revenue, other income, cost, deficit."""
    p25 = pub_adj / (fy25["fare_revenue"] * 1e6) - 1
    other = fy25["bus_and_bus_related_lease_income"] + (fy25.get("other_operating_income") or 0)
    rows = [{"fy": "FY2025/26", "level": 1.0, "rides": 1.0, "fare": fy25["fare_revenue"], "other": other,
             "cost": fy25["total_operating_expenditure"]}]
    full = 1 + p25
    for y in range(1, years + 1):
        step = dec_change if y == 1 else a["later_fre"]
        level = full * (1 + 95 / 365 * step)
        full = full * (1 + step)
        r = rows[-1]["rides"] * (1 + a["ridership_growth"])
        rows.append({"fy": f"FY{2025 + y}/{(26 + y) % 100:02d}", "level": level, "rides": r,
                     "fare": fy25["fare_revenue"] * level * r, "other": other,
                     "cost": rows[-1]["cost"] * (1 + a["cost_growth"])})
    for r in rows:
        r["deficit"] = r["fare"] + r["other"] - r["cost"]
    return rows


def model(rides, rep_km, sources, a, stats):
    """Every headline number of the workbook, from the same inputs."""
    cur, new = load_fares(CURRENT, sources), load_fares(APPROVED, sources)
    a = dict(derive(a), rep_km=rep_km)
    base = revenue(rides, cur, a)
    # The transfer factor is estimated once, at current fares, and applied to every scenario.
    appr = {"revenue": float((rides * blended(new, a["shares"])).sum()) / 100 * base["transfer_factor"]}
    fund = {r["period"]: r for r in sources["revenue_cost"] if str(r.get("entity", "")).startswith("LTA Bus Contracting")}
    fy25 = fund["financial year ended 31 March 2026"]
    published = fy25["fare_revenue"] * 1e6
    bus_share = fy25["fare_revenue"] / (fy25["fare_revenue"] + a["tel_fare_revenue_fy25"] / 1e6)
    adj = published + a["fy25_days_at_old_fares"] / 365 * a["fre2025_bus_tel_increase"] * bus_share
    lta_daily = a["lta_avg_daily_bus_ridership_2025"]
    out = {
        "annual_rides": base["rides"], "lta_annual_rides_2025": lta_daily * 365,
        "rides_ratio": base["rides"] / (lta_daily * 365) - 1,
        "adult_gross": base["adult_gross"], "gross": base["gross"], "transfer_factor": base["transfer_factor"],
        "rebate_cents": base["rebate_cents"], "mean_adult_cents": base["mean_adult_cents"],
        "revenue": base["revenue"], "bus_share": bus_share,
        "published_fy2025": published, "published_fy2025_at_dec2025_fares": adj,
        "revenue_vs_published_adj": base["revenue"] / adj - 1,
        "implied_yield": adj / base["adult_gross"],
        "approved_revenue": appr["revenue"], "approved_change": appr["revenue"] / base["revenue"] - 1,
        "approved_increase": appr["revenue"] - base["revenue"],
        "ptc_fre2026_bus": a["fre2026_bus_tel_increase"] * bus_share,
    }
    out["approved_increase_vs_ptc"] = out["approved_increase"] / out["ptc_fre2026_bus"] - 1
    flat, more = flat_fare(rides[:, 0], cur["adult"]["trunk"])
    out.update(flat_fare_cents=flat, flat_share_paying_more=more)
    out["concession_delta"] = concession_revenue(rides, cur, a, a["concession_change"]) - base["revenue"]
    out["scenarios"] = {(c, e): scenario_uniform(base["revenue"], 0, c, e)[0] / 1e6
                        for c in a["scenario_changes"] for e in a["scenario_elasticities"]}
    km = stats["fare_km_quantiles"]
    band = lambda x: min(N_BANDS - 1, max(0, math.ceil(round(round(x - 3.2, 1) - 1e-9, 9))))  # noqa: E731
    out["spend_median_now"] = monthly_spend(cur["adult"]["trunk"][band(km["0.5"])], a["commuter_rides_per_month"])
    out["spend_p90_dec"] = monthly_spend(new["adult"]["trunk"][band(km["0.9"])], a["commuter_rides_per_month"])
    out["spend_p90_dec_income"] = out["spend_p90_dec"] / a["income_p20"]
    out["shares"], out["transfer_share"] = a["shares"], a["transfer_share"]
    tendered = [c for c in sources["contracts"] if c.get("contract_type") == "tendered" and c.get("contract_value_sgd")]
    out["fee_service_year"] = (sum(c["contract_value_sgd"] / c["contract_years"] for c in tendered)
                               / sum(c["n_services"] for c in tendered))
    out.update(cost_recovery=fy25["fare_revenue"] / fy25["total_operating_expenditure"],
               fee_per_bus_km_floor=fy25["service_fees_and_incentives"] * 1e6 / (stats["weekday_bus_km"] * 365),
               deficit_per_ride=-fy25["deficit_before_government_grants"] * 1e6 / (lta_daily * 365))
    proj = projection(fy25, adj, out["approved_change"], a)
    out["projection"] = proj
    out["deficit_fy2030"] = proj[-1]["deficit"]
    out["fare_fy2030"] = proj[-1]["fare"]
    out["fare_fy2026"] = proj[1]["fare"]
    return out


if __name__ == "__main__":
    import finance_workbook
    finance_workbook.main()
