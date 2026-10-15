# data-collect

Raw LTA DataMall snapshots written by `.github/workflows/collect.yml` on `main`. This branch holds data only and
shares no history with `main`; the code that reads it lives there.

- `data/raw/speedbands/YYYY-MM-DD/sb_YYYYMMDD_HHMM.csv.gz`: one Traffic Speed Bands v4 snapshot per run
  (LinkID, SpeedBand, MinimumSpeed, MaximumSpeed), the same format as the laptop collector. The time in the name
  is Singapore time (UTC+08:00) at the minute the run started.
- `data/raw/speedbands/YYYY-MM-DD/manifest.csv`: one row per run, including failed pulls
  (snapshot_sgt, file, rows, sha256, bytes, pull_seconds, quota_retries, status).
- `data/raw/speedbands/links/speedband_links.csv.gz`: link geometry (LinkID, RoadName, RoadCategory, start and end
  coordinates), rewritten only when DataMall changes it.
- `data/raw/incidents/YYYY-MM-DD/inc_YYYYMMDD_HHMM.jsonl.gz` and `manifest.csv`: one Traffic Incidents poll per run.

Runs are every 15 minutes, 06:30-10:00 Singapore time, on weekdays that are not public holidays, plus two night
pulls every night at 01:10 and 03:10 for the free-flow night reference. GitHub starts
scheduled runs late at busy times, so the minutes in the file names are irregular; check the manifest for gaps.
Source: LTA DataMall.
