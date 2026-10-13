# Bus finance and fare model

Workbook: [`outputs/bus_finance_model.xlsx`](../outputs/bus_finance_model.xlsx). Built by `python src/finance_workbook.py`, which first derives the ride and bus-km inputs (`src/finance.py`) from the DataMall data in the pipeline. `src/finance_model.py` is the same model in Python. `tests/test_finance.py` evaluates the workbook's formulas with pycel and requires 28 headline cells and the scenario grid to match Python to 1e-9.

## Sheets

| Sheet | What it holds |
|---|---|
| Inputs | Published counts (blue) and the fare-group and transfer shares derived from them (formulas), plus projection and scenario inputs |
| Fares | PTC card fares by distance band: adult, senior/PwD, student and Workfare; trunk, express and feeder; in force from 27 Dec 2025 and approved for 26 Dec 2026 |
| Rides | Annual rides by fare class and distance band (July-August 2026 OD, scaled to 2026 day counts) |
| Revenue | Blended fare per band, revenue now and at the December 2026 tables, transfer factor, estimate |
| Transfers | 39 × 39 matrix of P(a) P(b) [F(a) + F(b) − F(a + b)]: the average fare a transfer leg does not pay |
| Reconciliation | Model against LTA's audited fare revenue, LTA ridership and PTC's fare-review revenue figure |
| Fund | LTA Bus Contracting accounts FY2020/21 to FY2025/26: fare revenue, service fees, operating cost, deficit, grants |
| Contracts | Nine tendered awards with published values: fee per year and per service-year |
| Projection | FY2026/27 to FY2030/31 fare revenue, cost and deficit before grants |
| Scenarios | Uniform fare changes × elasticity, a senior/PwD fare change, a revenue-neutral flat fare, a commuter's monthly spend against the adult pass and 20th-percentile household income |
| Sources | Every source with its URL and the quoted text |

## Steps and assumptions

1. **Fare distance.** For each origin-destination stop pair, the distance is the along-route distance on the services that call at both stops in order (BusRoutes cumulative distance).
   - A pair served by trunk, feeder and express services is charged the cheapest of their fares.
   - 0.4% of rides have no common service and use straight-line distance × 1.41, the median route/straight-line ratio.
2. **Annual rides.** Rides per day for each day type, the mean of July and August 2026, times the 2026 day counts.
3. **Fare-group shares.**
   - Seniors: 743,000 senior-card bus rides a day in 2019, out of 4,099,000 (MOT reply; LTA ridership).
   - Other concession groups: scaled by the 2021 concession-card mix, so seniors 47%, PwD 1%, students 31%, Workfare 9% and children 6% (MOT reply).
   - The scaling assumes the same rides per card in every concession group. Children under 7 ride free.
   - The concession-card mix and the 2019 senior rides are the latest published; neither is a 2026 measurement.
4. **Transfers.**
   - A typical 2018 weekday had 6.2 M trips forming 4.5 M journeys (bus and rail; academic preprint), so 27.4% of rides are taken to be transfer legs.
   - The rebate per transfer is the expected F(a) + F(b) − F(a + b) over two rides drawn from the observed adult trunk ride distribution.
   - The resulting transfer factor is applied to every scenario.
5. **Not modelled.**
   - Monthly and concession passes. About 126,000 monthly pass users (PTC); student concession passes are not counted.
   - Cash fares: under 1% of journeys.
   - Fare evasion.
   - Express surcharges on stop pairs also served by trunk services.

## Reconciliation

| Check | Model | Published | Gap |
|---|---|---|---|
| Annual bus rides | 1,452 M | 1,402 M (LTA 2025 average daily × 365) | +3.6% |
| Bus fare revenue, S$ a year | 1,264 M | 1,075 M (LTA FY2025/26 S$1,032 M plus the 2025 review's effect on the 270 days before it applied) | +17.6% |
| Extra revenue from the December 2026 review, S$ a year | 104 M | 90 M (PTC: S$97.1 M for bus + TEL, × 93.1% bus share of LTA's fare revenue) | +15.0% |

- **Level.** The model is 17.6% above the restated audited figure. Scaling its rides down to 2025 ridership (−3.6%) leaves +13.5%. That remainder is consistent with passes, which cap heavy users' fares, and with the fare-group shares being 2019 and 2021 proxies. The model does not claim to close the gap.
- **Fare changes.** The review increment is off by about the same proportion (+15.0%), so the model reproduces fare changes even though its level is high.

## Sources

The Sources sheet lists every source with its URL and quoted text. The main ones:

- **LTA.** Audited financial statements, Bus Contracting fund note, FY2020/21 to FY2025/26; Public Transport Ridership 2015-2025; contract award releases 2015-2026, with the 2015 Loyang and 2016 negotiated-contract values from secondary reproductions, flagged.
- **PTC.**
  - Fare Review Exercise 2024, 2025 and 2026 releases and annexes: fare tables, monthly passes, revenue effects and the affordability indicator.
  - Distance fares and transfer rules.
- **MOT.** Parliamentary replies on concession cards (2021), senior-card trips (2020), bus subsidies and contract extensions.
- **SingStat.** Table C030012: household market income by percentile, 2025.
- **Litman.** VTPI, *Transit Price Elasticities and Cross-Elasticities* (2025), Table 15.
- **Journeys.** Academic preprint arXiv 2509.13633, for trips per journey.
