# Singapore Bus Network Monitor

An open-data pipeline and dashboard for Singapore's public bus network. It covers:

- where services share the road;
- when and where people board;
- which stops changed from one month to the next;
- how many residents live within 400 m of a stop;
- which shared corridors are slow enough to screen for bus priority.

**Dashboard:** `docs/index.html` (GitHub Pages)

Data: LTA DataMall (September 2026 network, passenger volume for February, June, July and August 2026), URA Master Plan 2019, SingStat General Household Survey 2025 and OpenStreetMap. Everything is rebuilt from raw files by one command, and every table has recorded quality checks.

## Findings

**Shared corridors.**
- 7,845 directed stop-to-stop links make up the 799 service-directions that have routes.
- 396 links are shared by 10 or more services.
- 230 links are scheduled for 60 or more buses an hour in the weekday AM peak.
- The busiest link is Orchard Rd from Dhoby Ghaut Stn to Bencoolen Stn Exit B: 28 services and about 162 scheduled buses an hour.
- By road, the most kilometres of links carrying 10+ services are on Marine Parade Rd (6.0 km), Yio Chu Kang Rd (4.8 km) and Telok Blangah Rd (4.4 km).

**Monthly totals mislead unless divided by day counts.**
- Raw DataMall totals show weekday boardings falling 11.6% from July to August 2026.
- Per weekday they rose 1.6% (4.21 M to 4.28 M). July had 23 weekdays and August had 20, after National Day's Monday holiday.
- June, the school holiday month, is 10% below July per weekday.
- The pipeline uses the MOM 2026 holiday list for every comparison.

**Stops that changed.** Surveillance compares each stop's weekday boardings with the network-wide shift. A stop is flagged when it moves at least 3.5 robust standard deviations and at least 25% against the network.
- August vs July: 65 stops surged and 9 dropped.
  - 24 of the 65 surges are on the roads through NTU (Nanyang Dr, Ave and Cres) and NUS (Clementi Rd, Kent Ridge Cres), up to +152% at Academic Bldg Sth and +179% on Clementi Rd. This is consistent with term starting in August.
  - The Float @ Marina Bay fell 96%.
- August vs February: the largest surges are in Sengkang East (Blk 305D up 570%, Renjong Stn Exit B up 430%).
- These are leads to check against service changes, openings and events. They are not explanations.

**Walking coverage.**
- 98.5% of residents live within 400 m (straight line) of a bus stop.
- This comes from placing each subzone's GHS 2025 population on its Master Plan housing parcels, weighted by plot ratio.
- Spreading people evenly over each subzone gives 97.2% instead, and shows false gaps. Yishun appears at 93.7% because Lower Seletar Reservoir sits inside one of its subzones; on housing land it is 99.0%.
- The real gaps are the landed estates of Bukit Timah (76%) and Tanglin (82%), especially Hillcrest, Swiss Club and Leedon Park.
- None of Sentosa's housing parcels has a DataMall stop within 400 m.

**Bus priority screening.** Links shared by many buses are ranked by scheduled bus-hours lost per hour: AM peak buses per hour × km × (1/peak speed − 1/night speed), with speeds from LTA traffic speed bands. Speed-band snapshots are being collected every 15 minutes through weekday peaks. This section fills in once enough weekday AM peak and night snapshots exist.

## Method

| Step | Script | What it does |
|---|---|---|
| Fetch | `src/fetch_public.py`, `src/datamall.py` | OSM stops (Overpass), URA MP2019 planning areas, subzones and land use, SingStat table C020123; DataMall routes, services, stops, passenger volume and speed bands (key read from `LTA_KEY`, never stored) |
| Build | `src/build_db.py` | Loads everything into DuckDB, derives stop-to-stop links and runs 13 data-quality checks (`outputs/data_quality.csv`) |
| Corridors | `src/corridors.py` | Services and scheduled buses per hour on each directed link, from published headway bands |
| Demand | `src/demand.py` | Hourly profiles per stop and network, per-day normalisation, robust month-over-month surveillance |
| Coverage | `src/coverage.py` | 400 m catchments, population placed on housing parcels (dasymetric), results by subzone and planning area |
| Priority | `src/priority.py` | Matches 143,787 speed-band road segments to bus links, then computes peak and night speeds and bus-hours lost |
| Dashboard | `src/dashboard.py` | Static Plotly page in `docs/` |

The table and column definitions are in [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md).

### Validation

- **Stop positions:** LTA and OpenStreetMap agree to a median 6.5 m. 94.8% of the 5,194 stops found in both are within 50 m.
- **Speed-band matching:** a road segment is matched to a bus link by distance (25 m) and direction (within 40°).
  - Parallel roads were the main error: expressways beside their service roads, viaducts above arterials.
  - A road-name rule keeps only segments named like either stop's road. It raised name agreement from 76.8% to 98.0% of matched length.
- **Network integrity:**
  - Every route stop exists in the stop master.
  - Every route's service exists in the service list.
  - 236 of 26,842 route rows skip a sequence number.
  - 209 link lengths fall outside 0–5 km. 204 of them are non-stop sections of express and City Direct services (e.g. 646–649), and the others are stops that share a location or have distance errors.
  - One stop lies outside Singapore: Larkin Terminal in Johor Bahru, served by cross-border service 170.
- **Tests:** 24 unit tests cover headway parsing, day counts, bearings, road-name matching, the surveillance rule and the snapshot time windows.
  - `tests/mutate.py` makes 13 deliberate bugs in the code, and every one must fail the tests. It found three gaps in the first test set, which have since been closed.
  - CI runs both.

### Limits of the numbers

- Catchments are 400 m straight-line buffers. Walking distance along real paths is longer, so the true coverage is lower than reported.
- Headway bands are scheduled service, not observed buses.
- Speed bands describe general traffic. Buses also stop at bus stops.
- A one-month surveillance flag is a lead to investigate. It becomes a finding when the next month confirms it.

## Run it

```bash
pip install -r requirements.txt
python src/fetch_public.py                    # public data, no key needed
LTA_KEY=... python src/datamall.py static pv  # DataMall (free account key)
LTA_KEY=... bash src/sample_speed.sh 202609301000   # optional: speed-band snapshots every 15 min
python run_all.py                             # database, analyses, dashboard
python -m pytest -q tests && python tests/mutate.py
```

## Data sources

- LTA DataMall: Bus Routes, Bus Services, Bus Stops, Passenger Volume by Bus Stops, Traffic Speed Bands v4.
- URA Master Plan 2019 planning area boundaries, subzone boundaries and land use (data.gov.sg).
- SingStat General Household Survey 2025, table C020123: resident population by planning area and subzone.
- Ministry of Manpower public holidays 2026.
- OpenStreetMap contributors (bus stop positions, used as a cross-check).
- Basemap tiles: Esri Light Gray Canvas.

Raw data is not committed; `data/raw/` is rebuilt by the fetch scripts.
