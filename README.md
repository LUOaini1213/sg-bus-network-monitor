# Singapore Bus Network Monitor

An open-data pipeline and dashboard for Singapore's public bus network. It covers:

- where services share the road;
- when people board and where their trips go;
- which stops changed from one month to the next, and why;
- how many residents can reach a stop within 400 m;
- which shared corridors are slow enough to screen for bus priority.

**Dashboard:** https://luoaini1213.github.io/sg-bus-network-monitor/

Data:

- LTA DataMall: the September 2026 network; stop and origin-destination passenger volumes for February, June, July and August 2026; traffic speed bands.
- URA Master Plan 2019.
- SingStat General Household Survey 2025.
- OpenStreetMap.
- Official notices and calendars, used to verify causes.

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

- The walking-network figure is a lower bound. OSM misses many void-deck and covered-linkway shortcuts in HDB estates: the median walking-to-straight-line ratio comes out at 1.77, above typical urban values.
- The ranking is stable across all three measures. The lowest areas are the landed estates of **Tanglin** (45–82%) and **Bukit Timah** (50–76%), then Bishan and Tengah.
- Spreading residents evenly over each subzone instead of onto housing land creates false gaps. Yishun would appear at 93.7% because Lower Seletar Reservoir sits inside one of its subzones.

### Bus priority screening

Links are ranked by scheduled bus-hours lost per hour: AM peak buses per hour × km × (1/peak speed − 1/night speed), with speeds from LTA traffic speed bands. Each link needs at least four valid snapshots in each period. In each snapshot, segments with finite positive speeds and lengths must cover at least half the bus link's length; missing observations do not count as zero speed. The sampling script takes about 20 minutes per snapshot (a 15-minute pause after each 4-minute pull).

The reviewed build has **3,378 eligible links**, of which **2,637** have a positive estimated AM loss. Its AM data consists of **six snapshots on 28 September, 07:47–09:15**, compared with **50 night snapshots from 26 September to 3 October 2026** (Singapore time). This is one observed morning, not a representative multi-day delay estimate. Dates and counts are recorded alongside a hash of the displayed CSV in `outputs/priority_screen_metadata.json`; the dashboard shows a top-40 map and top-10 table.

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
  - 94 tests, and `tests/mutate.py`, which makes 28 deliberate bugs that must each fail at least one test. CI runs both.
  - The check has found test gaps twice, and both were closed:
    - the first test set let 3 of 13 bugs through;
    - the anomaly rules let 3 of 18 through.

### Limits of the numbers

- Straight-line catchments overstate access and the OSM walking network understates it. The two bracket the true figure.
- Headway bands describe scheduled service, not observed buses.
- Speed bands describe general traffic. Buses also stop at bus stops.
- Speed coverage is estimated from matched segment lengths, not a union of road geometry. Band midpoint speeds and the resulting priority scores are screening approximations; the current AM sample covers only one morning.
- A surveillance flag with no verified cause is a lead. It becomes a finding when a source or the next month confirms it.

## Run it

```bash
pip install -r requirements.txt
python src/fetch_public.py                          # public data, no key needed
curl -o data/raw/Singapore.osm.gz https://download.bbbike.org/osm/bbbike/Singapore/Singapore.osm.gz
LTA_KEY=... python src/datamall.py static pv        # DataMall (free account key)
LTA_KEY=... python src/datamall.py od 202608 202607  # origin-destination volumes
LTA_KEY=... bash src/sample_speed.sh 202609301000   # speed-band snapshots, about every 20 min
python run_all.py                                   # walking network (first run), database, analyses, dashboard
python -m pytest -q tests && python tests/mutate.py
```

## Data sources

- LTA DataMall: Bus Routes (pulled 2026-04-22 and 2026-09-26), Bus Services, Bus Stops, Passenger Volume by Bus Stops, Passenger Volume by Origin Destination Bus Stops, Traffic Speed Bands v4.
- URA Master Plan 2019 planning area boundaries, subzone boundaries and land use (data.gov.sg).
- SingStat General Household Survey 2025, table C020123.
- OpenStreetMap contributors: stop positions, and the walking network via the BBBike extract of 20 September 2026.
- Official notices and calendars (LTA, operators, NUS, NTU, SMU, polytechnics, ITE, MOE, MOM), listed with each event in `data/reference/network_events_2026.csv`.
- Basemap tiles: Esri Light Gray Canvas.

Raw data is not committed. `data/raw/` is rebuilt by the fetch commands above.
