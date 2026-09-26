# Data dictionary

The database file is `data/processed/sgbus.duckdb`, rebuilt by `python run_all.py`. Row counts are from the build of 2026-09-26.

## Database tables

### `stops` (5,209 rows): DataMall Bus Stops, with an OpenStreetMap cross-check
| Column | Type | Meaning |
|---|---|---|
| BusStopCode | text, 5 digits | LTA stop code, zero-padded (primary key) |
| RoadName, Description | text | LTA road and stop names (abbreviated, e.g. "Upp S'goon Rd") |
| Latitude, Longitude | float, WGS84 | LTA stop position |
| osm_lat, osm_lon | float | Position of the OSM `highway=bus_stop` node whose `ref` equals the stop code; null for 15 stops |
| shelter | text | OSM `shelter` tag, where mapped |

### `bus_services` (802 rows): DataMall Bus Services
| Column | Meaning |
|---|---|
| ServiceNo, Direction | Service and direction (1 or 2); key |
| Operator | SBST, SMRT, TTS or GAS |
| Category | TRUNK (587), FEEDER (113), CITY_LINK (68), EXPRESS (34) |
| OriginCode, DestinationCode | Terminal stop codes |
| AM_Peak_Freq, AM_Offpeak_Freq, PM_Peak_Freq, PM_Offpeak_Freq | Scheduled headway band in minutes, e.g. "09-12"; "-" means no service in that period |
| LoopDesc | Loop point for loop services |

### `bus_routes` (26,842 rows): DataMall Bus Routes
| Column | Meaning |
|---|---|
| ServiceNo, Direction, StopSequence | Key: the n-th stop of a service direction |
| BusStopCode | Stop served |
| Distance | Cumulative km from the first stop |
| WD/SAT/SUN_FirstBus, _LastBus | First and last bus times at this stop (HHMM) |

### `bus_links` (26,043 rows): derived
One row for each pair of consecutive stops on a service route.
| Column | Meaning |
|---|---|
| ServiceNo, Direction, StopSequence | Route row of the link's first stop |
| from_stop, to_stop | Consecutive stop codes |
| link_km | Difference in cumulative distance |

### `corridor_links` (7,845 rows): derived by `src/corridors.py`
One row per directed stop-to-stop link, aggregated over all the services that use it.
| Column | Meaning |
|---|---|
| from_stop, to_stop | Directed link (key) |
| services | Number of distinct services on the link |
| service_list | Service numbers, space-separated |
| link_km | Median link length across those services |
| AM_Peak_bph, AM_Offpeak_bph, PM_Peak_bph, PM_Offpeak_bph | Scheduled buses per hour, the sum over services of 60 / mid-point headway |
| bus_km_am_peak | AM_Peak_bph × link_km |
| from_* / to_* | Road name, stop name and coordinates of each end |

### `stop_volume` (812,444 rows): DataMall Passenger Volume by Bus Stops, 2026-02, 2026-06, 2026-07 and 2026-08
| Column | Meaning |
|---|---|
| YEAR_MONTH | "YYYY-MM" |
| DAY_TYPE | WEEKDAY or WEEKENDS/HOLIDAY |
| TIME_PER_HOUR | Hour of day (0–23; 14 rows fall outside this range and are excluded from analyses) |
| PT_CODE | Stop code |
| TOTAL_TAP_IN_VOLUME, TOTAL_TAP_OUT_VOLUME | Card taps summed over **all days of that type in the month** (divide by `day_counts()` in `src/demand.py`) |

### `speedband_match` (26,406 rows): derived by `src/priority.py`
Links each LTA speed-band road segment to the bus links it runs along.
| Column | Meaning |
|---|---|
| from_stop, to_stop | Bus link |
| LinkID | Speed-band segment ID (geometry in `data/raw/datamall/speedband_links.csv`) |
| RoadName, RoadCategory | Segment road name; category 1 = expressway |
| seg_m | Segment length in metres (SVY21) |
| seg_road | Normalised segment road name |
| same_road | True if the segment's road name matches either stop's road |

### `od_202607`, `od_202608` (DataMall Passenger Volume by Origin Destination Bus Stops)
One row per origin stop, destination stop, day type and hour.
| Column | Meaning |
|---|---|
| YEAR_MONTH, DAY_TYPE, hour | As in `stop_volume` |
| o, d | Tap-in and tap-out stop codes (5 characters) |
| trips | Trips summed over all days of the day type in the month |

### `stop_pa`
Each stop's URA MP2019 planning area and SVY21 position (x, y). The four stops in Johor Bahru are labelled "JOHOR BAHRU (cross-border)".

## Reference data (`data/reference/`, committed)

| File | Contents |
|---|---|
| `network_events_2026.csv` | Officially documented events used to verify anomaly causes: event_id, date_from, date_to, kind (stop closure, route change, renumbering, new service, academic term, school holiday), stops, services, description, source |
| `busroutes_20260422.csv.gz` | DataMall Bus Routes as pulled on 2026-04-22 (ServiceNo, Operator, Direction, StopSequence, BusStopCode, Distance), kept because DataMall serves only the current network |

## Processed walking network (`data/processed/`, rebuilt, not committed)

| File | Contents |
|---|---|
| `walk_nodes.parquet` | node (OSM id), x, y (SVY21) |
| `walk_edges.parquet` | u, v (OSM node ids), length_m. Walkable ways from the BBBike extract, using the osmnx `walk` filter |

## Outputs (`outputs/`)

| File | Grain | Key columns |
|---|---|---|
| `data_quality.csv` | one row per check | check, table, failing_rows, total_rows, pass_rate, note |
| `corridor_links.csv` | directed link | same as `corridor_links` table |
| `corridor_roads.csv` | road | links, km, max_services, mean/max_am_peak_bph (only links with 10+ services whose two stops are on the same road) |
| `network_hourly_profile.csv` | month × day type × hour | tap_in_per_day, tap_out_per_day (whole network) |
| `stop_profile_2026-08.csv` | stop | weekday_tap_in_per_day; am_peak_share (07–08 h); pm_peak_share (17–19 h); peak_hour; boarding_pattern (AM-peak boarding / PM-peak boarding / balanced) |
| `surveillance_2026-08_vs_<base>.csv` | stop | base = 2026-07, 2026-02 or baseline_median (the median of Feb, Jun and Jul); weekday boardings per day in the base month and in Aug; log_ratio; pct_change; network_shift; vs_network; robust_z; flag (surge / drop / blank) |
| `coverage_subzone.csv` / `.geojson` | MP2019 subzone | residents (GHS 2025); area_km2; area_covered; housing_parcels; housing_share_covered; method (dasymetric / even spread); residents_covered; residents_covered_even; stops |
| `coverage_planning_area.csv` | planning area | residents; resident_coverage; resident_coverage_even; residents_outside_400m; stops_per_10k_residents |
| `anomaly_evidence.csv` | flagged stop (median baseline) | monthly weekday series wd_2026-02 … wd_2026-08; services_added and services_removed (April → September, with renumbered services treated as unchanged); od_biggest_changes; near_campus; category; verified_event (event_id from the reference file) |
| `new_stops_2026.csv` | stop | stops with August boardings but no February or June record; services now and in April |
| `od_quality.csv` | month | OD weekday trips vs stop-volume tap-ins; difference_pct; trips with a stop missing from the master |
| `od_planning_area_2026-08.csv` | origin × destination planning area | trips_per_weekday |
| `od_top_pairs_2026-08.csv` | stop pair | trips_per_weekday, am_peak_trips (07:00–08:59) |
| `od_trip_length_2026-08.csv` | distance band | weekday trips by straight-line distance between the tap-in and tap-out stops |
| `od_flagged_stops.csv` | flagged stop × destination planning area | trips_jul, trips_aug, change |
| `coverage_measures.csv` | measure | share of residents reached: straight line 400 m, straight line 308 m, OSM walking network 400 m |
| `coverage_walk_planning_area.csv` / `coverage_walk_subzone.csv` | planning area / subzone | residents, coverage_line, coverage_line13, coverage_walk, gap_pts, residents_outside_walk |
| `priority_screen.csv` | bus link | services, AM_Peak_bph, link_km, match_ratio, v_peak and v_ref (km/h), bus_h_lost_per_h (written once there are 4+ weekday AM peak and 4+ night snapshots) |

## Conventions

- Stop codes are 5-character strings. Leading zeros matter: "01012" is a different code from 1012.
- Coordinates are stored in WGS84 (EPSG:4326). Distances and areas are computed in SVY21 (EPSG:3414).
- Day counts use the MOM 2026 public holidays. A Monday after a Sunday holiday (1 Jun, 10 Aug, 9 Nov) counts as a holiday.
- The speed for a band is its mid-point: band 1 = 0–9 km/h gives 5 km/h, and band 8 (70+) gives 75 km/h.
