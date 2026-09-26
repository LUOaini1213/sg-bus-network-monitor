#!/usr/bin/env bash
# Sample TrafficSpeedBands every 15 minutes until the given end time (YYYYmmddHHMM). Key comes from LTA_KEY.
END=${1:-202609301000}
cd "$(dirname "$0")/.."
while [ "$(date +%Y%m%d%H%M)" -lt "$END" ]; do
  python src/datamall.py speed >> data/raw/datamall/speedbands/sampler.log 2>&1
  sleep 900
done
