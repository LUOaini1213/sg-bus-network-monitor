# Singapore Bus Network Monitor

An open-data pipeline and dashboard for Singapore's public bus network. It covers:

- where services share the road;
- when people board and where their trips go;
- which stops changed from one month to the next, and why;
- how many residents can reach a stop within 400 m;
- which shared corridors are slow enough to screen for bus priority, and how stable that list is from morning to morning;
- how far ahead speed bands can be forecast on the road graph;
- how accurate the live bus arrival predictions are, and how regular the headways, on three trunk corridors;
- what public data says about bus safety;
- what the network earns and costs, and what fare changes would do.

**Dashboard:** https://luoaini1213.github.io/sg-bus-network-monitor/

Data:

- LTA DataMall: the September 2026 network; stop and origin-destination passenger volumes for February, June, July and August 2026; traffic speed bands; Bus Arrival (v3) at 41 corridor stops.
- URA Master Plan 2019.
- SingStat General Household Survey 2025.
- OpenStreetMap.
- Official notices and calendars, used to verify causes.
- Traffic Police, SingStat and Ministry of Transport road-safety statistics; LTA DataMall Traffic Incidents.
- PTC fare tables and fare reviews; LTA audited Bus Contracting accounts and contract awards.

Everything is rebuilt from raw files by one command, and every table has recorded quality checks.

## Findings

### Shared corridors

- 7,845 directed stop-to-stop links make up the 799 service-directions that have routes.
- 396 links are shared by 10 or more services, and 230 are scheduled for 60 or more buses an hour in the weekday AM peak.
- The busiest link is Orchard Rd from Dhoby Ghaut Stn to Bencoolen Stn Exit B: 28 services and about 162 scheduled buses an hour.
- By road, the most kilometres of links carrying 10+ services are on Marine Parade Rd (6.0 km), Yio Chu Kang Rd (4.8 km) and Telok Blangah Rd (4.4 km).

### Monthly totals mislead unless divided by day counts

- Raw DataMall totals show weekday boardings falling 11.6% from July to August 2026.
- Per weekday, boardings actually rose 1.6%, from 4.21 M to 4.28 M. July had 23 weekdays; August had 20, after the National Day Monday holiday.
- The pipeline uses the MOM 2026 holiday list for every comparison.

### Where trips go (origin-destination)

- About 4.28 M bus trips are made per weekday.
- 54% start and end in the same planning area, so buses mostly carry local and feeder trips.
- Trip lengths are short:
  - the median straight-line distance between tap-in and tap-out is 1.5 km;
  - 90% of trips are under 5.8 km.
- The busiest stop-to-stop flows cross the Causeway: Woodlands Checkpoint to Johor Bahru Checkpoint, about 22,400 trips a weekday.
- The busiest pairs of planning areas are Tampines and Bedok, about 28,700 trips each way.
- As a consistency check between the two DataMall products, OD trip totals match the stop tap-in totals to within 0.014% in both months.

### Stops that changed, and why

Each stop's August weekday boardings are compared with the **median of February, June and July**. The first version compared August with July alone, which made the polytechnics look like big drops: their July was a full teaching month and their August is exams.

Against the median baseline, 71 stops are flagged: 67 surges and 4 drops. Each flag is checked against three things: the monthly series, the services at the stop (DataMall routes, April against September) and official notices. See [`docs/ANOMALIES.md`](docs/ANOMALIES.md).

Verified causes:

- **The Float @ Marina Bay** fell 96% because LTA closed the stop from 4 August to 12 October 2026 while the Formula 1 race was being prepared.
- **The Jurong West St 75 reopening** on 19 July accounts for the new stops Gek Poh Shop Ctr and Blk 749, and for the drops at Blk 861 (−64%) and Blk 745 (−53%). The services 181/243/258/502/651 left those two stops.

Explained by calendars:

- 39 surges match the university and ITE terms. NUS and NTU started teaching on 10 August, SMU on 17 August and ITE on 13 July.
- 7 more follow the MOE school-term shape, with the June holidays sitting inside the baseline.

Still open: 4 sustained steps (e.g. Science Park II) and 14 stops have no verified cause. They stay as leads.

### Who can reach a stop within 400 m

Residents (GHS 2025) are placed on Master Plan housing parcels, weighted by plot ratio. Reach is then measured three ways:

| Measure | Residents covered |
|---|---|
| Straight line, 400 m (upper bound) | 98.5% |
| Straight line, 308 m (400 m with a 1.3 detour factor) | 96.7% |
| Along the OpenStreetMap walking network, 400 m (lower bound) | 79.7% |

The 79.7% is an archived measurement from the original graph builder. A separate [walking-graph review](docs/WALK_GRAPH_REVIEW_20261012.md) fixes duplicate-weight aggregation and exact zero-snap boundaries using actual Parquet fixtures. The original city inputs are unavailable in that review checkout, so the archived outputs remain unchanged and no corrected city-wide estimate is claimed.

- The walking-network figure is a lower bound. OSM misses many void-deck and covered-linkway shortcuts in HDB estates: the median walking-to-straight-line ratio comes out at 1.77, above typical urban values.
- The ranking is stable across all three measures. The lowest areas are the landed estates of **Tanglin** (45–82%) and **Bukit Timah** (50–76%), then Bishan and Tengah.
- Spreading residents evenly over each subzone instead of onto housing land creates false gaps. Yishun would appear at 93.7% because Lower Seletar Reservoir sits inside one of its subzones.

### Bus priority screening

Links are ranked by scheduled bus-hours lost per hour: AM peak buses per hour × km × (1/peak speed − 1/night speed), with speeds from LTA traffic speed bands. Each link needs at least four valid snapshots in each period. In each snapshot, segments with finite positive speeds and lengths must cover at least half the bus link's length; missing observations do not count as zero speed. The first sampler took a snapshot about every 20 minutes. The collector restarted on 13 October takes one every 15 minutes.

The screen runs morning by morning (`src/priority_days.py`):

- A link's speed on a morning is the median of that morning's covered snapshots between 07:30 and 09:30, with at least four of them.
- Its multi-morning AM speed is the median of those daily speeds, and it needs data on at least half the mornings.
- The night reference is every night snapshot in the archive: 89 from 26 September to 14 October 2026.

With one morning the code reproduces the earlier single-morning CSV exactly: same 3,378 links, same order, values equal to within 1e-13.

**Two weekday mornings so far.** The archive holds 28 September (six snapshots) and 14 October (nine). The mornings from 29 September to 6 October were lost when the old sampler stopped on PC restarts.

- **Eligible links.** 3,378 links are eligible on both mornings; 2,737 have a positive estimated loss on the combined screen.
- **Agreement between the two mornings.** Kendall's tau is **0.72** over all 3,378 links, but only **8 of the 20** top-ranked links are the same on both mornings (top-20 overlap 0.40). The broad ordering holds from one morning to the next; a top-20 list from a single morning does not.
- **Combined top 20.** The first seven are in both mornings' top 20: Jalan Eunos (Opp Blk 322 to Opp Blk 16), Upper East Coast Road before Bayshore Station, Loyang Avenue, Robinson Road, Yio Chu Kang Road, Yishun Avenue 2 and Shenton Way.
- **Network total.** Estimated bus-hours lost per hour, summed over eligible links: 107.2 on 28 September, 94.9 on 14 October.
- **Still to come.** Two mornings are too few for a stable shortlist. `python src/priority.py` re-ranks as the collector adds mornings, and `outputs/priority_stability.json` records the agreement for every pair of mornings.

The dates and snapshot counts are stored with a hash of the displayed CSV in `outputs/priority_screen_metadata.json`. The dashboard shows a top-40 map and a top-10 table.

### Short-term speed forecasts

`src/forecast.py` predicts speed bands 20, 40 and 60 minutes ahead from the last hour of snapshots.

- **Network.** 3,847 speed-band segments: the 2,598 expressway segments, plus the segments under bus links with 60 or more scheduled AM-peak buses an hour. 4,116 directed edges join each segment to the segments that start where it ends.
- **Models.**
  - Persistence (the last value).
  - A historical average by segment, day type and time of day.
  - LightGBM on own and neighbour lags.
  - A diffusion graph neural net (`src/forecast_gnn.py`, PyTorch, 185,827 parameters). It mixes two hops downstream, upstream and over a learned adjacency, and predicts the change from the last speed.
- **Split.** Whole days in time order: training 26–30 September (82 anchor slots), validation 1–2 October (49), test 14 October 00:00–09:40 (15, three of them in the AM peak).
- **Errors.** In km/h on band midpoints; the bands are 10 km/h wide.
- **Intervals.** 95% moving-block bootstrap over test slots. The same resamples are used for every model, so each model's paired difference from persistence gets its own interval.

| Model | 20 min MAE | 40 min MAE | 60 min MAE | 20 min RMSE |
|---|---|---|---|---|
| Persistence | 3.42 (2.94–3.54) | 3.97 (3.27–4.52) | 4.45 (3.55–5.29) | 7.28 |
| Historical average | 3.92 (2.95–4.32) | 4.05 (2.97–4.78) | 4.21 (2.97–5.19) | 8.31 |
| LightGBM | 3.35 (2.79–3.50) | 3.65 (2.85–4.18) | 4.11 (2.97–5.05) | 6.37 |
| Graph neural net, mean of 5 seeds | **3.12** (2.62–3.34) | 3.68 (2.84–4.41) | 4.14 (3.00–5.19) | 6.53 |

- **20 minutes.** The graph net's MAE is 0.13–0.50 km/h below persistence (paired interval). All five seeds beat persistence on their own, with single-seed MAEs of 3.09–3.29 (mean 3.16, s.d. 0.08). LightGBM has the lowest RMSE.
- **40 and 60 minutes.** LightGBM and the graph net are level, and both are below persistence. The historical average catches up with persistence at 60 minutes.
- **AM peak.** Only three test slots fall in it: the graph net 3.86, persistence 3.88, LightGBM 4.14. There is no gain where congestion forms.
- **How far this goes.** These numbers come from 15 test slots on one morning. The models were trained on 82 slots from five days of an irregular 18–20 minute archive, and graph-net training reached its 200-epoch cap. **This is not yet a credible general result.** The pipeline re-runs on the growing archive (`python src/forecast.py --test-from <date>`).

### Bus arrival predictions and bunching (first morning, 16 October 2026)

`src/busarrival.py` polls DataMall Bus Arrival at the 41 stops of three shared trunk corridors (Marine Parade Rd, Telok Blangah Rd, Orchard Rd) every 2 minutes, and `src/busarrival_analysis.py` scores the predictions against inferred arrivals.

**What the sample is.** One weekday morning: Friday 16 October 2026, 07:58–10:00 SGT. That is 61 sweeps and 2,501 calls, with no failed call and no quota fault. GitHub started the 06:00 scheduled run two hours late, so the morning covers the last half hour of the AM peak (to 08:30) and the start of the off-peak. Four speed-band pulls ran during it on the same key, with no quota retry. One morning is a first measurement, not a result. The numbers will be replaced as weekday mornings accumulate (`python src/busarrival_analysis.py --days ...`; outputs in `outputs/busarrival_*`). A manual afternoon run on 15 October (16:25–17:56) is the comparison below.

**How arrivals are inferred.** DataMall gives the next three predicted arrivals per service, but no vehicle id. Successive lists are aligned to follow each bus. A bus has arrived between two answers when its GPS-based (monitored) ETA crosses zero, or when it leaves the list while it was due. No arrival is inferred, and the headway sequence breaks, when a bus drops out while still more than 2 minutes away, when answers are more than 5 minutes apart, or when an answer is blank. At this cadence each arrival is known to ±1 minute. The full rule is in the module docstring.

- **Arrivals.** 5,813 inferred: 3,807 from a monitored ETA crossing zero and 2,006 from a bus leaving the list. 17 buses were dropped far from the stop.
- **ETA error by horizon.** 48,298 predictions. A prediction is scored only where the bus would still have been seen arriving 10 minutes late. Error is observed minus predicted, so positive means the bus came later than predicted.

| Horizon | Predictions | MAE | Bias | Within ±1 min | Within ±2 min | 90th pct. abs. error | 15 Oct afternoon MAE |
|---|---|---|---|---|---|---|---|
| 0–2 min | 5,603 | 39 s | +21 s | 83.2% | 98.0% | 79 s | 44 s |
| 2–5 min | 8,111 | 54 s | +19 s | 65.3% | 90.7% | 116 s | 53 s |
| 5–10 min | 12,771 | 73 s | +29 s | 49.6% | 82.4% | 150 s | 69 s |
| 10–20 min | 21,813 | 106 s | +50 s | 37.8% | 66.5% | 220 s | 99 s |

- **Buses run later than predicted at long horizons.** Arrivals from a monitored ETA crossing zero are almost unbiased up to 5 minutes out (+3 to +4 s). The bias then grows to +13 s at 5–10 minutes and +34 s at 10–20 minutes.
- **The inference rule moves the bias.** Arrivals inferred from a bus leaving the list carry +44 to +79 s of bias, because a bus leaves the list after it has dwelt and departed. Part of the overall positive bias is therefore the method, not the predictions. The 0–2 minute row also partly scores the system against its own at-the-stop estimate.
- **Monitored against scheduled.** 17.5% of listed buses were not monitored, so their ETA comes from the schedule. They are worse at every horizon: an MAE of 137 s at 10–20 minutes against 101 s for monitored buses, and 78 s against 37 s at 0–2 minutes.
- **Corridors.** The three corridors are within 5 s of each other up to 10 minutes. At 10–20 minutes Telok Blangah Rd is the worst (MAE 109 s), ahead of Orchard Rd (105 s) and Marine Parade Rd (103 s).
- **Headway regularity.** 5,229 observed headways. 483 service-stop pairs have 4 or more. Their median coefficient of variation is **0.41**: 0.42 on Marine Parade Rd, 0.43 on Orchard Rd and 0.39 on Telok Blangah Rd. The afternoon's was 0.42.
- **Bunching.** **6.0%** of headways were shorter than a quarter of the scheduled headway for the period. That is 5.1% of the 1,067 AM-peak headways and 6.3% in the off-peak. By corridor: 5.8% on Marine Parade Rd, 5.8% on Orchard Rd and 6.4% on Telok Blangah Rd. About 98% of headways had a scheduled reference. Against the median observed headway instead, the share is 4.2%. The afternoon's share was 5.7%.
- **Data quality.**
  - 15 of the 2,501 answers (0.6%) were HTTP 200 with no service listed at all; the afternoon had 27 of 1,845. On 15 October the stops around these blanks had listed a median of 12 services. Treated as "every bus left", the blanks had produced 116 false "lost" buses in the first 16 minutes of that afternoon's data. They are now treated as missing answers.
  - When a service was listed, its first ETA was never empty.
  - Load codes: 91.9% "seats available" (SEA), 8.1% "standing available" (SDA) and 0.01% "limited standing" (LSD), against 94.7% / 5.3% / 0 in the afternoon.
  - Every monitored bus had a position; unmonitored ones report 0.0, 0.0.
  - Bus types: 58.2% single-deck, 41.7% double-deck, 0.1% bendy.

### Bus finance and fares

An Excel workbook, [`outputs/bus_finance_model.xlsx`](outputs/bus_finance_model.xlsx), models the money side of the Bus Contracting Model from public sources. Inputs are blue; every other cell is a live formula. `src/finance_model.py` computes the same numbers in Python, and the tests evaluate the workbook's formulas and require them to agree to 1e-9. Method and sources: [`docs/BUS_FINANCE.md`](docs/BUS_FINANCE.md).

Revenue is estimated bottom-up:

- **Rides.** July and August 2026 origin-destination rides are scaled to 2026 day counts (251 weekdays, 114 weekend days and holidays): **1,452 M rides a year**. That is 3.6% above LTA's published 2025 average daily bus ridership × 365.
- **Fare distance.** Each stop pair is priced at the along-route distance on the cheapest service that calls at both stops. 99.6% of rides are on a pair with a common service.
- **Fares.** PTC card fare tables in force from 27 December 2025, for adult, senior/PwD, student and Workfare fares.
- **Fare groups.** In 2019 there were 743,000 senior-card bus rides a day out of 4.10 M. Scaling that by the concession-card mix gives 18.5% senior/PwD, 12.0% student, 3.5% Workfare, 2.3% free (children) and 63.7% adult. This assumes the same rides per card in every concession group.
- **Transfers.** Distance fares are charged per journey, so 27.4% of rides are treated as transfer legs (6.2 M trips made 4.5 M journeys in 2018). Each transfer leg loses the average F(a) + F(b) − F(a + b) of 121 cents.

The resulting model figures, against what LTA and PTC publish:

| | Model | Published | Gap |
|---|---|---|---|
| Bus fare revenue, S$ a year | 1,264 M | 1,075 M (LTA FY2025/26 audited S$1,032 M, restated to December 2025 fares) | +17.6% |
| Extra revenue from the December 2026 fare review | 104 M (+8.2%) | 90 M (PTC's S$97.1 M for bus + TEL, bus share) | +15.0% |

- The revenue gap is the size expected from what the model leaves out. Monthly and concession passes cap heavy users' fares, and the OD data is from 2026 while the accounts are for FY2025/26. The model does not claim to close it.
- The two gaps agree (+17.6% and +15.0%), so the fare-change mechanics hold even where the level is high.
- PTC granted 7.0% overall across bus and rail. On this ride mix the approved bus tables raise bus revenue by 8.2%: 68% of rides are 3.2 km or less, and that band rises most (adult $1.28 to $1.40, +9.4%).
- If every ride paid the adult fare with no transfers, revenue would be S$2,041 M. The published figure is 52.7% of that.

From LTA's audited accounts and contract awards:

- **Cost recovery.** FY2025/26 bus fare revenue covered **51.5%** of the S$2,005 M operating cost.
- **Deficit.** The deficit before grants was S$813 M, **S$0.58 per ride** at 2025 ridership.
- **Contract fees.** The nine tendered awards with published values average **S$4.13 M per service-year** (estimated total fee ÷ term ÷ services).
- **Cost per bus-km.** Service fees per scheduled timetable bus-km come to S$5.24 per bus-km. This is indicative only: the timetable estimate (910,618 bus-km per weekday) leaves out dead running.

Fare scenarios (Scenarios sheet):

- **Uniform +5%.** Revenue rises from S$1,264 M to S$1,327 M with no ridership response. With elasticity −0.2 it reaches S$1,314 M; with −0.5, S$1,295 M. The elasticities are generic short-run values (VTPI), not Singapore estimates.
- **Senior/PwD fares −10%.** About −S$15.9 M a year.
- **Revenue-neutral flat adult fare.** 141.9 cents on trunk services. 73% of trunk rides would pay more, because most rides are short.
- **Affordability.** A commuter making 44 rides a month pays S$56.32 a month at the median ride (2.2 km). At the 90th-percentile ride (8.2 km) the bill is S$82.72 at December 2026 fares, against the S$122 adult pass. That is 1.9% of 20th-percentile household market income (S$4,329).
- **Projection.** FY2026/27 to FY2030/31 uses the reviews' part-year timing, ridership +1.24% a year (2023–2025 trend) and operating cost +1.94% a year (FY2020/21–FY2025/26 trend), with no fare review after 2026. On those inputs the deficit before grants is S$828 M in FY2030/31. Every one of these inputs is a blue cell.

### Bus safety brief

[`docs/BUS_SAFETY_BRIEF.md`](docs/BUS_SAFETY_BRIEF.md) is a one-page bulletin built by `src/safety.py` from Traffic Police, SingStat and LTA tables, Ministry of Transport statements and the live DataMall incident feed. Rates carry exact Poisson 95% intervals.

- **Buses involved per registered bus.** 462 buses were involved in fatal or injury accidents in 2025: **24.6 per 1,000 registered buses** (95% CI 22.4–26.9). The 2017–2019 rate was 25.2, giving a rate ratio of 0.97 (0.88–1.08). The post-COVID rise has returned to the pre-COVID level, not above it.
- **Fatal involvements.** 18 in 2025 against 14 a year in 2022–2024: rate ratio 1.26 (0.68–2.23). There are too few events to call a change.
- **Public buses only.** The Taskforce counted 191, 200 and 185 serious collisions in 2022–2024. Per ride, 2024 against 2022 is 0.87 (0.71–1.07). The bus captain was at fault in 45–50% of them.
- **Exposure gap.** Official bus-km stops in 2014. The 2017 statements imply about 1.19 M bus-km a day, so a public-bus accident rate per bus-km for 2022–2025 cannot be rebuilt from public data.
- **Live incidents (DataMall).** 167 distinct incidents were reported from 13 October 21:44 to 14 October 09:40. 124 of them were on expressways, which few bus routes use. Off expressways, 6 of 43 (14%, 95% CI 5–28%) fell on bus-route segments, which make up 23% of non-expressway road length. Nine were accidents, and none was on a bus-route segment. Half a day of data supports no conclusion; the archive grows with the collector.

### Reliability review, 3 October 2026

- Filtering anomaly flags before deduplicating stops restores OD evidence for **14 median-baseline-only stops**. All **71 median-flagged stops** now have OD evidence, up from 57. Their categories and the underlying monthly counts are unchanged. `flag_baseline` identifies which comparison supplied the flag; the OD change itself remains July → August.
- Regression fixtures cover missing/invalid speeds, insufficient coverage and per-link sample counts. The saved real-data window retains the same 3,378 eligible links as the previous algorithm; the fixtures demonstrate the edge cases rather than claiming the current sample had false congestion.
- A run with insufficient observations now replaces previous rankings with an empty result and records the actual observation window. Smoke output is separate. Missing data is not presented as proof of no congestion.
- The mutation check first requires the full suite to pass and verifies each changed module can compile/import. Test collection or environment failures no longer count as detected bugs.

See [review evidence and reproduction details](docs/REVIEW_20261003.md).

## Method

| Step | Script | What it does |
|---|---|---|
| Fetch | `src/fetch_public.py`, `src/datamall.py` | OSM stops, URA MP2019 boundaries and land use, SingStat C020123; DataMall routes, services, stops, passenger volume (stop and OD) and speed bands. The key is read from `LTA_KEY` and never stored. |
| Walking network | `src/walk_network.py` | Streams the BBBike OSM extract and keeps walkable ways (the osmnx `walk` filter): 825,460 segments |
| Build | `src/build_db.py` | Loads everything into DuckDB, derives stop-to-stop links and runs 13 data-quality checks |
| Corridors | `src/corridors.py` | Services and scheduled buses per hour on each directed link |
| Demand | `src/demand.py` | Per-day normalisation, hourly profiles, robust surveillance against a single month or a multi-month median |
| OD | `src/od.py` | Planning-area matrix, top pairs, trip lengths, flows from flagged stops, and the OD vs tap-in check |
| Anomalies | `src/anomalies.py` | Evidence table per flagged stop: monthly series, service changes (renumbering aware), OD changes, category, and verified events from `data/reference/network_events_2026.csv` |
| Coverage | `src/coverage.py`, `src/coverage_walk.py` | Dasymetric population; straight-line and walking-network reach |
| Priority | `src/priority.py` | Matches 143,787 speed-band segments to bus links, then computes peak and night speeds and bus-hours lost |
| Collector | `src/collect.py`, `.github/workflows/collect.yml` | Speed bands every 15 minutes and Traffic Incidents every 2 minutes, written atomically; `status` and `stop` subcommands. A full snapshot is 288 paged calls; at 5- and 10-minute cadences DataMall's call quota cut pulls off after 5-7 snapshots an hour. From 16 October 2026 GitHub Actions takes the weekday-morning and nightly snapshots (`gate`, `next` and `once` subcommands; one looping job per morning) and commits them to the `data-collect` branch |
| Bus Arrival | `src/busarrival_stops.py`, `src/busarrival.py`, `.github/workflows/busarrival.yml` | Picks the 41 stops of three shared corridors from the corridor analysis, then polls DataMall v3/BusArrival at each one every 2 minutes on weekday mornings: one call every 2.9 s, never in bursts, with quota backoff. Sweeps go to the `data-collect` branch with a manifest |
| Arrival accuracy | `src/busarrival_analysis.py` | Infers observed arrivals from successive next-bus lists, then reports ETA error by prediction horizon, headway regularity (coefficient of variation), bunching and data quality |
| Speed cache | `src/speed_cache.py` | One compressed matrix per day of band speeds for the 39,017 links the analyses use; rebuilt only for days whose files changed |
| Priority by morning | `src/priority_days.py` | The priority rule morning by morning, the median across mornings, and rank stability between mornings |
| Forecasts | `src/forecast.py`, `src/forecast_gnn.py` | Persistence, historical average, LightGBM and a diffusion graph neural net on the expressway + bus-corridor road graph |
| Safety | `src/fetch_safety.py`, `src/safety.py` | Accident trends and rates with exact intervals; live incidents against the bus network; writes `docs/BUS_SAFETY_BRIEF.md` |
| Finance | `src/finance.py`, `src/finance_model.py`, `src/finance_workbook.py` | Fare distance and annual rides from OD, scheduled bus-km, the fare and cost model, and the Excel workbook |
| Dashboard | `src/dashboard.py` | Static Plotly page in `docs/` |

Table and column definitions are in [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md).

### Validation

- **Stop positions:** LTA and OpenStreetMap agree to a median 6.5 m. 94.8% of the stops found in both are within 50 m of each other.
- **Speed-band matching:** a road-name rule removes parallel-road errors, such as expressways beside their service roads. It raised name agreement from 76.8% to 98.0% of matched length.
- **OD against stop volumes:** the totals agree to within 0.014% in July and in August.
- **Walking network:**
  - 15% of OSM walking nodes sit in 4,015 small fragments that are not connected to the street network.
  - Points are therefore snapped only to components with 1,000 or more nodes. Without this, coverage came out at 75.3% instead of 79.7%.
- **Network integrity:**
  - Every route stop exists in the stop master, and every route's service exists in the service list.
  - 236 of 26,842 route rows skip a sequence number.
  - One stop lies outside Singapore: Larkin Terminal in Johor Bahru, on cross-border service 170.
- **Tests:**
  - 294 tests, and `tests/mutate.py`, which makes 130 deliberate bugs that must each fail at least one test. CI runs both.
  - The 97 mutants added for the collector (eight of them for the GitHub Actions morning and night windows, holidays, quota backoff and the quota exit, and 12 for the morning loop's slots, backups and stop), the multi-morning screen, the forecasts, the safety brief, the finance model and the Bus Arrival collector and analysis (25) run only the test files for their module.
  - The finance tests evaluate the workbook's formulas without Excel (pycel) and compare 28 headline cells and the scenario grid with the Python model.
  - The check has found test gaps three times, and each was closed:
    - the first test set let 3 of 13 bugs through;
    - the anomaly rules let 3 of 18 through;
    - the tests for this round's modules let 2 of 51 through: the AM window within a morning, and the cache's link alignment. A third mutant looped forever against the paging test double, which now ends.
    - the first Bus Arrival tests let 1 of 23 through: the tie rule in the list alignment. A fixture with a tie now pins it.
    - the morning-loop tests let 1 of 12 through: a manual run's first pull must ignore a pull made just before it.

### Limits of the numbers

- Straight-line catchments overstate access and the OSM walking network understates it. The two bracket the true figure.
- Headway bands describe scheduled service, not observed buses.
- Speed bands describe general traffic. Buses also stop at bus stops.
- Speed coverage is estimated from matched segment lengths, not a union of road geometry. Band midpoint speeds and the resulting priority scores are screening approximations; the AM sample covers two mornings (28 September and 14 October 2026).
- The finance model's fare-group and transfer shares are 2018–2021 proxies, and monthly and concession passes are not modelled. Its revenue is 17.6% above LTA's restated audited figure, and the README reports that gap rather than tuning it away.
- Traffic Police bus counts cover all bus types, and there is no public bus-km series after 2014, so public-bus accident rates per bus-km after 2017 cannot be rebuilt.
- Bus Arrival publishes predictions only, so observed arrivals are inferred from when a bus leaves the next-bus list or its monitored ETA crosses zero. At a 2-minute cadence each arrival is known to about ±1 minute, which is coarse against the 0–2 minute horizon, and part of that horizon scores the system against its own at-the-stop estimate. Headways and bunching come from these inferred arrivals, not from vehicle logs or the operators' fleet management systems.
- A surveillance flag with no verified cause is a lead. It becomes a finding when a source or the next month confirms it.

## Run it

```bash
pip install -r requirements.txt
python src/fetch_public.py                          # public data, no key needed
curl -o data/raw/Singapore.osm.gz https://download.bbbike.org/osm/bbbike/Singapore/Singapore.osm.gz
LTA_KEY=... python src/datamall.py static pv        # DataMall (free account key)
LTA_KEY=... python src/datamall.py od 202608 202607  # origin-destination volumes
LTA_KEY=... python src/collect.py run --until 202610302359   # speed bands every 15 min, incidents every 2 min
git fetch origin data-collect                       # weekday-morning and night snapshots from GitHub Actions (from 16 Oct 2026)
git worktree add ../sgbus-data origin/data-collect  # Bus Arrival sweeps are under data/raw/busarrival/
python src/busarrival_analysis.py --data ../sgbus-data/data/raw/busarrival   # ETA accuracy, headways, bunching
python src/fetch_safety.py                          # SingStat / data.gov.sg safety and ridership tables
python run_all.py                                   # walking network (first run), database, analyses, dashboard
python src/forecast.py                              # forecasts (PyTorch: pip install torch)
python -m pytest -q tests && python tests/mutate.py
```

## Data sources

- LTA DataMall: Bus Routes (pulled 2026-04-22 and 2026-09-26), Bus Services, Bus Stops, Passenger Volume by Bus Stops, Passenger Volume by Origin Destination Bus Stops, Traffic Speed Bands v4.
- URA Master Plan 2019 planning area boundaries, subzone boundaries and land use (data.gov.sg).
- SingStat General Household Survey 2025, table C020123.
- OpenStreetMap contributors: stop positions, and the walking network via the BBBike extract of 20 September 2026.
- Official notices and calendars (LTA, operators, NUS, NTU, SMU, polytechnics, ITE, MOE, MOM), listed with each event in `data/reference/network_events_2026.csv`.
- Basemap tiles: Esri Light Gray Canvas.

- Traffic Police (Singapore Police Force): Traffic Annual 2025 and Road Traffic Accident Statistics 2004-2016. SingStat tables M651281, M651351 and C030012. LTA vehicle statistics MVP01-1 and data.gov.sg d_120bba76c28dd4706e0cf85693827e64 (daily bus-km, 2004-2014). Ministry of Transport replies and the Bus Safety Tripartite Taskforce report. Quoted text and URLs are in `data/reference/safety/public_bus_figures.csv`.
- PTC fare review annexes 2024-2026, and the fares, passes and transfer-rule pages. LTA audited financial statements FY2020/21-FY2025/26 and bus contract award releases. MOT parliamentary replies on concession cards and senior-card trips. VTPI fare elasticities. Every source, with its quoted text, is in `data/reference/finance_sources.json`, `data/reference/finance_assumptions.json` and the workbook's Sources sheet.
- LTA DataMall Traffic Incidents, archived by the collector from 13 October 2026.
- LTA DataMall Bus Arrival v3, archived at 41 stops from 15 October 2026.

Raw data is not committed. `data/raw/` is rebuilt by the fetch commands above.

### The collector

`src/collect.py` runs for weeks without a terminal.

- **Start.** Run it as its own process so that closing a session does not stop it. In PowerShell, from the repository root: set `$env:LTA_KEY`, then `Start-Process python -ArgumentList "src/collect.py","run","--until","202610302359" -WindowStyle Hidden`, then clear the variable. The key is read once and never written anywhere.
- **Status.** `python src/collect.py status` shows the process id, the latest snapshot and the end of the log.
- **Stop.** `python src/collect.py stop` writes a stop file, and the collector exits within about 10 seconds. You can also stop the process whose id is in `data/raw/datamall/collector.pid`.
- **Limits.** Sleep or a reboot stops the collector. Gaps show up in the file list and in `collector.log`.

From 16 October 2026 the weekday-morning and night snapshots are taken by GitHub Actions instead (`.github/workflows/collect.yml`):

- **When.** Every 15 minutes from 06:30 to 10:00 Singapore time, Monday to Friday, from **one job per morning that loops** (changed on 16 October 2026). That morning GitHub ran only 2 of the 15 scheduled 15-minute runs (08:36 and 08:50 SGT): it delays and drops scheduled runs. Now one trigger at 06:20 SGT (22:20 UTC, Sunday to Thursday) starts a job that waits for 06:30, then pulls, commits and pushes, and sleeps until the next slot on the 06:30 + 15-minute grid, until the 10:00 pull (timeout 240 minutes). Two backup triggers at 06:47 and 07:13 SGT run in the same concurrency group, so they cannot run beside the loop. A backup that runs after the loop finds every slot already in the day's manifest and pulls nothing. A backup that starts first, because the first trigger was dropped, runs the loop itself: it pulls at once, then joins the grid. Slots are planned by `python src/collect.py next`: a slot within 7.5 minutes of a pull already in the manifest is skipped, and a slot that has passed during a long pull or a quota wait is not caught up. Two more runs every night, at 01:10 and 03:10 (17:10 and 19:10 UTC), pull once each for the priority screen's 22:00-05:00 night reference. `python src/collect.py gate` decides in Singapore time and skips morning pulls on the public holidays in `src/demand.py`. A manual run (`workflow_dispatch`) always pulls: `-f iterations=2 -f every_minutes=3` makes two pulls 3 minutes apart, and `-f dry_run=true` runs the schedule without calling DataMall or committing.
- **What.** `python src/collect.py once` makes one speed-band pull and one incident poll per slot. A "Rate limit quota violation" waits 60 and then 120 seconds before the same page is asked for again (three tries in all). A pull still refused for quota is written to the day's manifest as failed, shows as a warning on the run, and the loop carries on to the next slot. Any other failed pull, or a missing key, fails the run when the loop ends, so it shows in the Actions list.
- **Where.** The `data-collect` branch, which shares no history with `main`. Each pull is one commit, pushed at once, so a failure later in the morning loses nothing: `data/raw/speedbands/YYYY-MM-DD/sb_YYYYMMDD_HHMM.csv.gz` (the same format as above) and a `manifest.csv` row with the time, row count and SHA-256, plus the incident poll under `data/raw/incidents/YYYY-MM-DD/`. Link geometry is kept once in `data/raw/speedbands/links/`.
- **Key.** The repository secret `LTA_ACCOUNT_KEY` is passed to that one step as `LTA_KEY`. It is not printed or written to the branch.
- **Stop.** Disable the workflow: Actions > collect speed bands > Disable workflow, or `gh workflow disable collect.yml -R LUOaini1213/sg-bus-network-monitor` (`gh workflow enable` resumes it). The data already on `data-collect` stays.
- **Limits.** GitHub starts scheduled runs late when its runners are busy, sometimes by two hours, and can drop runs. If all three morning triggers are late, the morning starts late, and the manifest is the record of what arrived. A morning run that starts more than 20 minutes after 10:00 is skipped; one that starts between 10:00 and 10:20 makes one pull, unless the manifest already has a pull in the last 15 minutes. The holiday list covers 2026 only; in 2027 the collector warns and collects every weekday until the list is extended.

The Bus Arrival sample has its own workflow (`.github/workflows/busarrival.yml`, from 15 October 2026):

- **Which stops.** `python src/busarrival_stops.py` writes `data/reference/busarrival_stops.csv`: every stop at an end of a 10+-service link on Marine Parade Rd (19 stops; the most kilometres of such links), Telok Blangah Rd (13; third) and Orchard Rd (9; the busiest single link). Yio Chu Kang Rd, second by length, is left out to keep the call budget down. `data/reference/busarrival_services.csv` lists the 656 stop-services with their scheduled headway bands from the 26 September 2026 BusServices pull.
- **When.** One job per weekday morning. Cron starts it at 06:00 SGT, with backup triggers at 06:27, 06:53 and 07:19 in the same concurrency group (on 16 October the 06:00 run started at 07:58). It waits for 06:30 and sweeps until 10:00, committing to `data-collect` every 15 minutes so a cancelled run keeps what it pushed. `python src/busarrival.py gate` skips weekends and the public holidays in `src/demand.py`. A manual run polls for the number of minutes given (`gh workflow run busarrival.yml -f minutes=20`).
- **Call budget.** One call per stop, one at a time, spread evenly over the 120-second sweep: one call every 2.9 s, 1,230 calls an hour, about 4,300 a morning. DataMall does not publish its quota; speed-band pulls failed at 1,440–2,016 calls an hour made in bursts of four parallel requests, while a 2-minute incident poll kept working through the same hours. A quota fault (HTTP 429, or an error body that mentions the quota) ends the sweep, records the remaining stops as skipped, pauses 60, then 120, then 240 s, and doubles the period up to 480 s; five clean sweeps halve it again. On 15 October a speed-band pull (288 calls) ran during the manual Bus Arrival run, and neither saw a quota fault.
- **Where.** `data/raw/busarrival/YYYY-MM-DD/ba_YYYYMMDD_HHMMSS.jsonl.gz`, one line per stop with the arrival time of the answer and DataMall's `Services` list as returned, and a `manifest.csv` row per sweep (period, calls ok / failed / skipped / quota, services listed, SHA-256).
- **Key.** The same `LTA_ACCOUNT_KEY` secret, passed only to the polling step and sent only as the `AccountKey` header.
- **Stop.** `gh workflow disable busarrival.yml -R LUOaini1213/sg-bus-network-monitor`.
