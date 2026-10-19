"""Collector logic that runs unattended for weeks: paging, atomic snapshot files, schedule ticks, incident log."""
import datetime as dt
import gzip
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import collect  # noqa: E402
import speed_cache  # noqa: E402


class FakeSession:
    """Serves `total` rows in pages of 500."""

    def __init__(self, total):
        self.total, self.calls = total, []

    def get(self, url, params, headers, timeout):
        skip = params["$skip"]
        self.calls.append(skip)
        # Like DataMall: short page at the end, empty page exactly at the end. Past the end this fake returns
        # junk, so a collector that keeps reading after the first short page is caught.
        n = min(500, self.total - skip) if skip <= self.total else (500 if skip < self.total + 5000 else 0)
        base = skip if skip <= self.total else 10 ** 6
        rows = [{"LinkID": base + i, "SpeedBand": 3, "MinimumSpeed": 20, "MaximumSpeed": 29} for i in range(n)]

        class R:
            status_code, text = 200, ""

            def raise_for_status(self):
                pass

            def json(self):
                return {"value": rows}
        return R()


@pytest.mark.parametrize("total, workers", [(1120, 4), (1120, 1), (1000, 2), (499, 3)])
def test_fetch_all_stops_at_the_first_short_page(total, workers):
    s = FakeSession(total)
    rows = collect.fetch_all(s, "k", "v4/TrafficSpeedBands", workers=workers)
    assert len(rows) == total
    assert [r["LinkID"] for r in rows] == list(range(total))


def test_write_snapshot_is_atomic_and_matches_the_sampler_format(tmp_path):
    rows = [{"LinkID": 7, "SpeedBand": 2, "MinimumSpeed": 10, "MaximumSpeed": 19, "RoadName": "X"}]
    path = collect.write_snapshot(rows, dt.datetime(2026, 10, 14, 8, 5), directory=tmp_path)
    assert path.name == "sb_20261014_0805.csv.gz"
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temporary file left behind
    with gzip.open(path, "rt") as z:
        d = pd.read_csv(z)
    assert d.columns.tolist() == ["LinkID", "SpeedBand", "MinimumSpeed", "MaximumSpeed"]
    assert d.iloc[0].tolist() == [7, 2, 10, 19]
    assert speed_cache.snapshot_time(path) == dt.datetime(2026, 10, 14, 8, 5)


def test_incident_polls_append_one_line_each(tmp_path):
    t = dt.datetime(2026, 10, 14, 8, 2)
    collect.append_incidents([{"Type": "Accident", "Message": "a"}], t, directory=tmp_path)
    collect.append_incidents([], t + dt.timedelta(minutes=2), directory=tmp_path)
    lines = (tmp_path / "inc_20261014.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["n"] for x in lines] == [1, 0]
    assert json.loads(lines[1])["polled_at"] == "2026-10-14T08:04:00"


@pytest.mark.parametrize("now, every, expected", [
    (dt.datetime(2026, 10, 14, 8, 3, 10), 300, dt.datetime(2026, 10, 14, 8, 5)),
    (dt.datetime(2026, 10, 14, 8, 5, 0), 300, dt.datetime(2026, 10, 14, 8, 10)),
    (dt.datetime(2026, 10, 14, 8, 5, 0), 120, dt.datetime(2026, 10, 14, 8, 6)),
])
def test_next_tick_is_the_next_clock_multiple(now, every, expected):
    assert collect.next_tick(now, every) == expected


def test_band_midpoints_and_missing_bands():
    v = speed_cache.band_kmh([1, 3, 8, 0], [0, 20, 70, np.nan], [9, 29, 999, np.nan])
    np.testing.assert_allclose(v, [5.0, 25.0, 75.0, np.nan], equal_nan=True)


def test_speed_cache_aligns_links_and_rebuilds_only_changed_days(tmp_path):
    raw, cache = tmp_path / "raw", tmp_path / "cache"
    raw.mkdir()

    def snap(when, rows):
        collect.write_snapshot([{"LinkID": l, "SpeedBand": b, "MinimumSpeed": 10 * (b - 1), "MaximumSpeed": 10 * b - 1}
                                for l, b in rows], when, directory=raw)

    snap(dt.datetime(2026, 10, 14, 8, 0), [(30, 2), (10, 4)])           # unordered, link 20 missing
    snap(dt.datetime(2026, 10, 15, 8, 0), [(99, 5), (10, 1), (20, 3)])  # link 99 is not watched
    links = np.array([10, 20, 30])
    files = speed_cache.build(raw, cache, links, verbose=False)
    t, li, k = speed_cache.load(cache=cache)
    assert li.tolist() == [10, 20, 30]
    np.testing.assert_allclose(k, [[35.0, np.nan, 15.0], [5.0, 25.0, np.nan]], equal_nan=True)
    assert [str(x) for x in t] == ["2026-10-14T08:00", "2026-10-15T08:00"]
    first = {f.name: f.stat().st_mtime_ns for f in files}
    snap(dt.datetime(2026, 10, 15, 8, 5), [(10, 2)])
    files = speed_cache.build(raw, cache, links, verbose=False)
    assert files[0].stat().st_mtime_ns == first["20261014.npz"]
    t, _, k = speed_cache.load(cache=cache, links=[30, 10])
    assert len(t) == 3
    np.testing.assert_allclose(k[:, 1], [35.0, 5.0, 15.0])


def test_quota_fault_is_not_retried():
    class Quota:
        calls = 0

        def get(self, url, params, headers, timeout):
            Quota.calls += 1

            class R:
                status_code = 500
                text = '{"fault":{"faultstring":"Rate limit quota violation. Quota limit  exceeded."}}'

                def raise_for_status(self):
                    raise collect.requests.HTTPError("500")
            return R()
    with pytest.raises(collect.QuotaExceeded):
        collect.get_page(Quota(), "k", "v4/TrafficSpeedBands", 0)
    assert Quota.calls == 1


# ---------------------------------------------------------------- GitHub Actions collector: window, holidays, cron
UTC = dt.timezone.utc
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "collect.yml"


@pytest.mark.parametrize("now, expected", [
    (dt.datetime(2026, 10, 19, 6, 30), True),             # Monday, first pull
    (dt.datetime(2026, 10, 19, 6, 29), False),
    (dt.datetime(2026, 10, 23, 10, 0), True),             # Friday, last pull
    (dt.datetime(2026, 10, 23, 10, 20), True),            # a late start still belongs to the morning
    (dt.datetime(2026, 10, 23, 10, 21), False),
    (dt.datetime(2026, 10, 23, 13, 0), False),
    (dt.datetime(2026, 10, 24, 1, 10), True),             # night pulls run every night, weekends included
    (dt.datetime(2026, 11, 9, 3, 10), True),              # and on holidays
    (dt.datetime(2026, 10, 20, 22, 0), True),
    (dt.datetime(2026, 10, 20, 21, 59), False),
    (dt.datetime(2026, 10, 21, 5, 0), True),
    (dt.datetime(2026, 10, 21, 5, 1), False),
    (dt.datetime(2026, 10, 24, 17, 10, tzinfo=UTC), True),   # Sunday 01:10 SGT
    (dt.datetime(2026, 10, 24, 8, 0), False),             # Saturday
    (dt.datetime(2026, 10, 25, 8, 0), False),             # Sunday
    (dt.datetime(2026, 11, 9, 8, 0), False),              # Monday after Deepavali (holiday in lieu)
    (dt.datetime(2026, 12, 25, 8, 0), False),             # Christmas, a Friday
    (dt.datetime(2026, 10, 18, 22, 30, tzinfo=UTC), True),   # Sunday night UTC is Monday 06:30 SGT
    (dt.datetime(2026, 10, 19, 6, 30, tzinfo=UTC), False),   # 06:30 UTC is 14:30 SGT
    (dt.datetime(2026, 10, 23, 2, 0, tzinfo=UTC), True),     # Friday 10:00 SGT
    (dt.datetime(2026, 10, 23, 22, 30, tzinfo=UTC), False),  # Friday night UTC is Saturday morning SGT
    (dt.datetime(2026, 11, 8, 23, 0, tzinfo=UTC), False),    # 07:00 SGT on the 9 November holiday
])
def test_collection_window_is_weekday_mornings_in_singapore_time_without_holidays(now, expected):
    assert collect.collection_window(now)[0] is expected


def test_night_window_is_the_priority_screens_night_reference():
    import priority
    assert collect.NIGHT_WINDOW == priority.NIGHT


def test_collection_window_uses_the_given_holiday_list_and_warns_past_its_years():
    monday = dt.datetime(2026, 10, 19, 8, 0)
    assert collect.collection_window(monday, holidays={dt.date(2026, 10, 19)})[0] is False
    ok, reason = collect.collection_window(dt.datetime(2027, 1, 4, 8, 0))
    assert ok and "2027" in reason


def _cron_field(text, lo, hi):
    out = set()
    for part in text.split(","):
        rng, _, step = part.partition("/")
        a, b = (lo, hi) if rng == "*" else map(int, (rng.split("-") * 2)[:2])
        out |= set(range(a, b + 1, int(step or 1)))
    return out


def _cron_fires(expr, start, end):
    """UTC times between start and end matched by a 5-field cron with *, lists, ranges and steps (no dom/month)."""
    minute, hour, dom, month, dow = expr.split()
    assert dom == "*" and month == "*"
    mins, hours, days = _cron_field(minute, 0, 59), _cron_field(hour, 0, 23), _cron_field(dow, 0, 6)
    t, out = start, set()
    while t < end:
        if t.minute in mins and t.hour in hours and (t.isoweekday() % 7) in days:
            out.add(t)
        t += dt.timedelta(minutes=1)
    return out


GITHUB_DELAY = dt.timedelta(hours=2)   # 19 October 2026: the morning runs started 1 h 54 min to 1 h 58 min late
GITHUB_MAX_TIMEOUT = 360               # minutes, GitHub-hosted runners


def test_workflow_cron_starts_one_morning_loop_with_three_backups_and_two_night_pulls():
    import re
    text = WORKFLOW.read_text(encoding="utf-8")
    crons = re.findall(r'cron:\s*"([^"]+)"', text)
    assert collect.NIGHT_CRON in crons
    start = dt.datetime(2026, 10, 18, tzinfo=UTC)  # a Sunday; one full week
    fires = set().union(*(_cron_fires(c, start, start + dt.timedelta(days=7)) for c in crons))
    sgt = sorted(f.astimezone(collect.SGT).replace(tzinfo=None) for f in fires)
    mornings = [dt.datetime(2026, 10, d, h, m) for d in range(19, 24) for h, m in ((4, 10), (5, 2), (5, 51), (6, 41))]
    nights = [dt.datetime(2026, 10, d, h, 10) for d in range(19, 26) for h in (1, 3)]  # 17:10, 19:10 UTC
    assert sgt == sorted(mornings + nights)
    for c in crons:
        night = collect.trigger_of(c) == "night"
        for f in _cron_fires(c, start, start + dt.timedelta(days=7)):
            f = f.astimezone(collect.SGT).replace(tzinfo=None)
            if night:    # a night trigger pulls once
                assert collect.loop_plan(f, trigger="night", job_start=f)[:3] == (True, f, f)
                continue
            # every morning trigger runs the whole 06:30-10:00 loop, on time or two hours late
            for late in (dt.timedelta(0), GITHUB_DELAY):
                assert collect.loop_plan(f + late, trigger="morning", job_start=f + late)[:3] == (
                    True, f.replace(hour=6, minute=30), f.replace(hour=10, minute=0))
    first = min(f for f in mornings if f.day == 19)
    assert first + GITHUB_DELAY <= dt.datetime(2026, 10, 19, 6, 30)   # even two hours late, before 06:30
    assert "LTA_KEY: ${{ secrets.LTA_ACCOUNT_KEY }}" in text and "cancel-in-progress: false" in text


def test_workflow_timeouts_and_the_gate_inputs():
    import re
    text = WORKFLOW.read_text(encoding="utf-8")
    cron, night_min, morning_min = re.search(
        r"timeout-minutes: \$\{\{ github\.event\.schedule == '([^']+)' && (\d+) \|\| (\d+) \}\}", text).groups()
    assert cron == collect.NIGHT_CRON and f'cron: "{cron}"' in text
    assert int(night_min) == collect.NIGHT_TIMEOUT_MIN and int(morning_min) == collect.JOB_TIMEOUT_MIN
    assert collect.JOB_TIMEOUT_MIN < GITHUB_MAX_TIMEOUT
    assert collect.MAX_RUN_MIN + collect.LAST_PULL_MIN <= collect.JOB_TIMEOUT_MIN
    # the last pull's budget covers the slowest pull seen (72 s on 19 October) plus the whole quota backoff
    assert collect.LAST_PULL_MIN * 60 >= 72 + sum(collect.QUOTA_BACKOFF)
    # the gate is told which cron fired and when the job started (the first step)
    steps = text.split("    steps:\n", 1)[1]
    assert steps.lstrip().startswith("- name: Job start (for the timeout bound)\n        id: t0\n")
    assert 'run: echo "epoch=$(date +%s)" >> "$GITHUB_OUTPUT"' in steps
    assert "SCHEDULE: ${{ github.event.schedule }}" in text and "JOB_START: ${{ steps.t0.outputs.epoch }}" in text
    assert 'collect.py gate --schedule "$SCHEDULE" --job-start "$JOB_START" ${{' in text


def test_gate_step_outputs_follow_the_cron_and_the_job_start():
    def epoch(t):
        return str(int(t.timestamp()))
    primary = dt.datetime(2026, 10, 18, 20, 11, tzinfo=UTC)   # Monday 04:11 SGT
    ok, _, out = collect.gate(primary, schedule="10 20 * * 0-4", job_start=epoch(primary - dt.timedelta(minutes=1)))
    assert ok and (out["start"], out["until"], out["day"]) == ("2026-10-19T06:30", "2026-10-19T10:00", "2026-10-19")
    assert (out["collect"], out["every"]) == ("true", collect.LOOP_EVERY)
    early = dt.datetime(2026, 10, 18, 20, 0, tzinfo=UTC)      # a job that started at 04:00 SGT
    assert collect.gate(primary, schedule="10 20 * * 0-4", job_start=epoch(early))[2]["until"] == "2026-10-19T09:50"
    assert collect.gate(primary, schedule="10 20 * * 0-4")[2]["until"] == "2026-10-19T10:00"
    late_night = dt.datetime(2026, 10, 18, 20, 31, tzinfo=UTC)   # the 03:10 trigger, started at 04:31 SGT
    out = collect.gate(late_night, schedule=collect.NIGHT_CRON, job_start=epoch(late_night))[2]
    assert (out["start"], out["until"]) == ("2026-10-19T04:31", "2026-10-19T04:31")
    out = collect.gate(late_night, schedule="10 20 * * 0-4", job_start=epoch(late_night))[2]
    assert (out["start"], out["until"]) == ("2026-10-19T06:30", "2026-10-19T10:00")
    ok, _, out = collect.gate(late_night + dt.timedelta(hours=1), schedule=collect.NIGHT_CRON)
    assert not ok and (out["collect"], out["start"], out["until"]) == ("false", "", "")
    ok, _, out = collect.gate(late_night, force=True, iterations=2, every=180)
    assert ok and (out["start"], out["until"], out["every"]) == ("2026-10-19T04:31", "2026-10-19T04:34", 180)


def test_trigger_of_the_cron_that_fired():
    assert collect.trigger_of(collect.NIGHT_CRON) == "night"
    assert collect.trigger_of("10 20 * * 0-4") == "morning"
    assert collect.trigger_of("") is None and collect.trigger_of(None) is None


# ---------------------------------------------------------------- the morning loop: plan, slots, stop
M = dt.datetime(2026, 10, 19)   # a Monday


def hm(h, m, s=0, day=M):
    return day.replace(hour=h, minute=m, second=s)


@pytest.mark.parametrize("now, trigger, plan", [
    (hm(6, 20), None, (True, hm(6, 30), hm(10, 0))),
    (hm(5, 30), None, (True, hm(6, 30), hm(10, 0))),       # after the night window: a morning run
    (hm(4, 10), "morning", (True, hm(6, 30), hm(10, 0))),  # the primary trigger: waits 140 minutes
    (hm(4, 0), "morning", (True, hm(6, 30), hm(9, 50))),   # 150 minutes early: ends 350 minutes after it started
    (hm(3, 59), "morning", (False, None, None)),           # before the allowance
    (hm(8, 36), "morning", (True, hm(6, 30), hm(10, 0))),  # a late start still loops to 10:00
    (hm(10, 20), None, (True, hm(6, 30), hm(10, 0))),
    (hm(10, 21), "morning", (False, None, None)),
    (hm(4, 10, day=dt.datetime(2026, 10, 24)), "morning", (False, None, None)),   # Saturday
    (hm(4, 10, day=dt.datetime(2026, 11, 9)), "morning", (False, None, None)),    # holiday
    (hm(1, 10, 40), None, (True, hm(1, 10), hm(1, 10))),   # night: one pull
    (hm(4, 30), None, (True, hm(4, 30), hm(4, 30))),       # inside the night window, cron unknown: a night pull
    (hm(4, 30), "night", (True, hm(4, 30), hm(4, 30))),    # a 03:10 trigger GitHub started late
    (hm(5, 0), "night", (True, hm(5, 0), hm(5, 0))),
    (hm(5, 1), "night", (False, None, None)),              # too late for the night reference: nothing
    (hm(8, 14), "night", (False, None, None)),
    (hm(3, 10, day=dt.datetime(2026, 10, 24)), "night", (True, hm(3, 10, day=dt.datetime(2026, 10, 24)),
                                                         hm(3, 10, day=dt.datetime(2026, 10, 24)))),
])
def test_loop_plan(now, trigger, plan):
    assert collect.loop_plan(now, trigger=trigger)[:3] == plan


def test_a_loop_from_04_10_waits_for_06_30_and_pulls_to_10_00():
    ok, start, until, _ = collect.loop_plan(hm(4, 11), trigger="morning", job_start=hm(4, 10, 2))
    assert (ok, start, until) == (True, hm(6, 30), hm(10, 0))
    assert collect.next_pull(hm(4, 11, 20), start, until, first=True) == hm(6, 30)
    now = [hm(4, 11, 20)]
    collect.wait_until(hm(6, 30), clock=lambda: now[0],
                       sleep=lambda s: now.__setitem__(0, now[0] + dt.timedelta(seconds=s)))
    assert now[0] == hm(6, 30)
    made = run_loop(hm(4, 11, 20), until=until)
    assert made == [hm(6, 30) + dt.timedelta(minutes=15 * k) for k in range(15)]


def test_the_last_pull_is_within_the_job_timeout_for_every_start():
    for minute in range(3 * 60 + 30, 10 * 60 + 30):
        t = hm(minute // 60, minute % 60)
        for setup in (0, 2):   # the gate runs a minute or two after the job started
            began = t - dt.timedelta(minutes=setup)
            ok, start, until, _ = collect.loop_plan(t, trigger="morning", job_start=began)
            if not ok:
                assert t < hm(4, 0) or t > hm(10, 20)
                continue
            assert t >= hm(6, 30) - dt.timedelta(minutes=collect.EARLY_MIN)
            made = run_loop(t, until=until)
            assert made and made[0] == max(hm(6, 30), t)
            ran = (made[-1] - began).total_seconds() / 60 + collect.LAST_PULL_MIN
            assert ran <= collect.JOB_TIMEOUT_MIN
            if began >= hm(4, 10):   # the primary cron fires at 04:10, so no job starts earlier
                assert made[-1] > hm(9, 45)   # the 10:00 pull, or a late start's own pull after 09:52


def test_a_job_started_too_early_for_the_10_00_pull_leaves_it_to_a_backup():
    ok, start, until, reason = collect.loop_plan(hm(4, 2), trigger="morning", job_start=hm(4, 0))
    assert (ok, until) == (True, hm(9, 50)) and "09:50" in reason
    made = run_loop(hm(4, 2), until=until)
    assert made[-1] == hm(9, 45)
    # the backup that waited in the concurrency group starts when the job ends and makes the 10:00 pull
    assert collect.loop_plan(hm(9, 46), trigger="morning")[:3] == (True, hm(6, 30), hm(10, 0))
    assert run_loop(hm(9, 46, 30), made) == [hm(10, 0)]


def test_a_late_night_run_cannot_hold_the_concurrency_group_into_the_morning():
    # the latest night run that pulls starts at the end of the night window and is stopped NIGHT_TIMEOUT_MIN later
    latest = dt.datetime.combine(M.date(), collect.NIGHT_WINDOW[1])
    assert collect.loop_plan(latest, trigger="night")[0]
    assert not collect.loop_plan(latest + dt.timedelta(minutes=1), trigger="night")[0]
    freed = latest + dt.timedelta(minutes=collect.NIGHT_TIMEOUT_MIN)
    assert freed <= hm(5, 30)   # the waiting morning job then starts with an hour to spare
    assert collect.loop_plan(freed, trigger="morning", job_start=freed)[:3] == (True, hm(6, 30), hm(10, 0))


def test_manual_loop_plan_runs_the_given_number_of_pulls():
    ok, start, until, _ = collect.loop_plan(hm(14, 3, 20), force=True, iterations=2, every=180)
    assert (ok, start, until) == (True, hm(14, 3), hm(14, 6))
    assert collect.next_pull(hm(14, 3, 30), start, until, 180, first=True, force=True) == hm(14, 3)
    # a manual run pulls at once even right after another pull
    assert collect.next_pull(hm(14, 3, 30), start, until, 180, done=[hm(14, 1)], first=True, force=True) == hm(14, 3)
    assert collect.next_pull(hm(14, 3, 55), start, until, 180, done=[hm(14, 3)]) == hm(14, 6)
    assert collect.next_pull(hm(14, 6, 50), start, until, 180, done=[hm(14, 3), hm(14, 6)]) is None


def run_loop(now, done=(), until=hm(10, 0)):
    """The pulls a morning loop makes from `now`, each taking 50 s, given pulls already in the manifest."""
    done, made, first = list(done), [], True
    while (t := collect.next_pull(now, hm(6, 30), until, 900, done, first)) is not None:
        made.append(t)
        done.append(t)
        now, first = t + dt.timedelta(seconds=50), False
    return made


def test_a_loop_from_before_the_window_pulls_the_15_minute_grid_to_10_00():
    made = run_loop(hm(6, 20, 5))
    assert made == [hm(6, 30) + dt.timedelta(minutes=15 * k) for k in range(15)]


def test_a_late_loop_pulls_at_once_then_joins_the_grid():
    assert run_loop(hm(8, 36, 12)) == [hm(8, 36), hm(8, 45), hm(9, 0), hm(9, 15), hm(9, 30), hm(9, 45), hm(10, 0)]


def test_a_backup_after_the_loop_pulls_nothing():
    done = [hm(6, 30) + dt.timedelta(minutes=15 * k) for k in range(15)]
    assert run_loop(hm(10, 1), done) == []
    assert run_loop(hm(10, 12), done) == []           # within 15 minutes of the 10:00 pull
    assert run_loop(hm(10, 16), [hm(9, 0)]) == [hm(10, 16)]   # GitHub started the only run late: one pull


def test_a_slot_another_job_took_is_skipped_and_a_passed_slot_is_not_caught_up():
    assert collect.next_pull(hm(6, 40), hm(6, 30), hm(10, 0), 900, done=[hm(6, 30), hm(6, 45)]) == hm(7, 0)
    assert collect.next_pull(hm(6, 47, 30), hm(6, 30), hm(10, 0), 900, done=[hm(6, 30)]) == hm(7, 0)
    assert collect.next_pull(hm(6, 45), hm(6, 30), hm(10, 0), 900, done=[hm(6, 30)]) == hm(6, 45)


def test_done_pulls_reads_only_successful_rows(tmp_path):
    m = tmp_path / "manifest.csv"
    assert collect.done_pulls(m) == []
    collect.append_manifest(m, {"snapshot_sgt": "2026-10-19T06:30", "status": "ok"})
    collect.append_manifest(m, {"snapshot_sgt": "2026-10-19T06:45", "status": "failed: QuotaExceeded: x"})
    collect.append_manifest(m, {"snapshot_sgt": "2026-10-19T07:00", "status": "ok (expected 143787)"})
    assert collect.done_pulls(m) == [hm(6, 30), hm(7, 0)]


def test_wait_until_sleeps_to_the_slot():
    now = [hm(6, 20, 5)]
    collect.wait_until(hm(6, 30), clock=lambda: now[0],
                       sleep=lambda s: now.__setitem__(0, now[0] + dt.timedelta(seconds=s)))
    assert now[0] == hm(6, 30)


# ---------------------------------------------------------------- one pull per run: quota backoff and manifest
class QuotaThenRows(FakeSession):
    """Answers the first `faults` calls with DataMall's quota fault, then serves pages like FakeSession."""

    def __init__(self, total, faults):
        super().__init__(total)
        self.faults = faults

    def get(self, url, params, headers, timeout):
        if self.faults:
            self.faults -= 1
            self.calls.append(params["$skip"])

            class R:
                status_code, text = 429, '{"fault":{"faultstring":"Rate limit quota violation."}}'

                def raise_for_status(self):
                    raise collect.requests.HTTPError("429")
            return R()
        return super().get(url, params, headers, timeout)


def test_quota_fault_waits_with_backoff_then_asks_for_the_same_page(monkeypatch):
    waits, retries = [], []
    monkeypatch.setattr(collect.time, "sleep", waits.append)
    monkeypatch.setattr(collect, "ECHO", True)
    s = QuotaThenRows(1120, faults=2)
    rows = collect.fetch_all(s, "k", "v4/TrafficSpeedBands", workers=1, backoff=(60, 120, 240), retries=retries)
    assert [r["LinkID"] for r in rows] == list(range(1120))
    assert waits == [60, 120] and retries == [0, 0]
    assert s.calls[:3] == [0, 0, 0]


def test_quota_fault_after_the_last_backoff_is_raised(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    monkeypatch.setattr(collect, "ECHO", True)
    with pytest.raises(collect.QuotaExceeded):
        collect.fetch_all(QuotaThenRows(10, faults=3), "k", "v4/TrafficSpeedBands", workers=1, backoff=(1, 2))


def test_once_writes_one_snapshot_one_incident_poll_and_manifests(tmp_path, monkeypatch):
    import hashlib

    class Session(FakeSession):
        def get(self, url, params, headers, timeout):
            r = super().get(url, params, headers, timeout)
            if url.endswith("TrafficIncidents"):
                r.json = lambda: {"value": [{"Type": "Accident", "Message": "x"}] if params["$skip"] == 0 else []}
            else:
                rows = r.json()["value"]
                for x in rows:
                    x.update(RoadName="R", RoadCategory=1, StartLon=103.8, StartLat=1.3, EndLon=103.81, EndLat=1.31)
                r.json = lambda: {"value": rows}
            return r
    monkeypatch.setenv("LTA_KEY", "secret-key")
    monkeypatch.setattr(collect.requests, "Session", lambda: Session(1200))
    monkeypatch.setattr(collect, "ECHO", True)
    stamp = dt.datetime(2026, 10, 19, 7, 15)
    assert collect.once(tmp_path, stamp, expected_links=1200) == 0
    day = tmp_path / "data" / "raw" / "speedbands" / "2026-10-19"
    snap = day / "sb_20261019_0715.csv.gz"
    m = pd.read_csv(day / "manifest.csv")
    assert m.columns.tolist() == collect.MANIFEST_COLUMNS
    assert m.loc[0, "rows"] == 1200 and m.loc[0, "status"] == "ok"
    assert m.loc[0, "sha256"] == hashlib.sha256(snap.read_bytes()).hexdigest()
    assert m.loc[0, "snapshot_sgt"] == "2026-10-19T07:15"
    inc = tmp_path / "data" / "raw" / "incidents" / "2026-10-19"
    with gzip.open(inc / "inc_20261019_0715.jsonl.gz", "rt") as z:
        assert json.loads(z.read())["n"] == 1
    assert pd.read_csv(inc / "manifest.csv").loc[0, "rows"] == 1
    links = tmp_path / "data" / "raw" / "speedbands" / "links" / "speedband_links.csv.gz"
    assert pd.read_csv(links).columns.tolist() == collect.LINK_COLUMNS
    before = links.stat().st_mtime_ns
    assert collect.once(tmp_path, stamp + dt.timedelta(minutes=15), expected_links=1200) == 0
    assert links.stat().st_mtime_ns == before  # unchanged geometry is not rewritten
    assert len(pd.read_csv(day / "manifest.csv")) == 2
    assert not any("secret-key" in p.read_bytes().decode("latin-1") for p in tmp_path.rglob("*") if p.is_file())


def test_default_quota_backoff_is_three_tries_of_the_same_page(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    monkeypatch.setattr(collect, "ECHO", True)
    s = QuotaThenRows(10, faults=2)  # third try answers
    assert len(collect.fetch_all(s, "k", "v4/TrafficSpeedBands", workers=1, backoff=collect.QUOTA_BACKOFF)) == 10
    assert s.calls[:3] == [0, 0, 0]
    s = QuotaThenRows(10, faults=3)  # three refusals: give up, no fourth call
    with pytest.raises(collect.QuotaExceeded):
        collect.fetch_all(s, "k", "v4/TrafficSpeedBands", workers=1, backoff=collect.QUOTA_BACKOFF)
    assert s.calls == [0, 0, 0]


def test_once_records_a_failed_pull_in_the_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LTA_KEY", "k")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    monkeypatch.setattr(collect.requests, "Session", lambda: QuotaThenRows(10, faults=10 ** 6))
    monkeypatch.setattr(collect, "ECHO", True)
    # out of quota after the backoff: logged as a warning, not counted, so the run exits 0
    assert collect.once(tmp_path, dt.datetime(2026, 10, 19, 8, 0), backoff=(1, 2)) == 0
    out = capsys.readouterr().out
    assert "::warning::speedbands pull" in out and "::error::" not in out
    m = pd.read_csv(tmp_path / "data" / "raw" / "speedbands" / "2026-10-19" / "manifest.csv")
    assert m.loc[0, "status"].startswith("failed: QuotaExceeded") and m.loc[0, "rows"] == 0
    assert m.loc[0, "quota_retries"] >= 2  # two waits on the first page; parallel pages may add their own
    assert not list((tmp_path / "data" / "raw" / "speedbands" / "2026-10-19").glob("sb_*"))


def test_missing_key_is_an_actions_error_annotation_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LTA_KEY", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(collect, "ECHO", True)
    with pytest.raises(SystemExit):
        collect.once(tmp_path, dt.datetime(2026, 10, 19, 8, 0))
    assert "::error::LTA_KEY is empty" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_a_failed_pull_that_is_not_the_quota_fails_the_run(tmp_path, monkeypatch, capsys):
    class Down(FakeSession):
        def get(self, url, params, headers, timeout):
            class R:
                status_code, text = 503, "Service Unavailable"

                def raise_for_status(self):
                    raise collect.requests.HTTPError("503")
            return R()
    monkeypatch.setenv("LTA_KEY", "k")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    monkeypatch.setattr(collect.requests, "Session", lambda: Down(10))
    monkeypatch.setattr(collect, "ECHO", True)
    assert collect.once(tmp_path, dt.datetime(2026, 10, 19, 8, 0)) == 2
    assert "::error::speedbands pull" in capsys.readouterr().out


def test_a_manual_run_always_pulls():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch" in text
    assert '--job-start "$JOB_START" ${{ github.event_name == \'workflow_dispatch\' && \'--force --iterations' in text
    ok, start, until, _ = collect.loop_plan(dt.datetime(2026, 10, 24, 13, 0), force=True)   # Saturday afternoon
    assert ok and start == until
