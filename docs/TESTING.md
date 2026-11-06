# Tests and the mutation check

`tests/mutate.py` writes each deliberate bug (mutant) into a copy of the source, runs the tests and requires at least one to fail. It first requires the unmodified full suite to pass, compiles or imports every mutated module, and counts only real test failures, so collection or environment errors are not counted as caught bugs.

## Which tests a mutant runs

The 151 mutants added for the collector (eight of them for the GitHub Actions morning and night windows, holidays, quota backoff and the quota exit, 11 for the morning loop's slots, backups and stop, 22 for the early triggers, the timeout bound and telling night runs from morning ones, and 15 for the night loop, nine of these 37 in `collect.yml` itself), the multi-morning screen, the forecasts, the safety brief, the finance model, the Bus Arrival collector and analysis (37, four in `busarrival.yml`) and the 23 October rerun (six: day folders, the rebuilt inputs and the whole-day bootstrap) run only the test files for their module. A mutant of a workflow file is tested, not compiled.

The mutants added on 6 November 2026 cover the review fixes: a gap after a bus already seen arriving breaks the headways (the fix of PR #2), the 10-minute end margin applies to each unbroken run of answers, runs split at gaps longer than the limit, the answer-gap check behind `max_answer_gap_in_headway_s` and the stop-code tie order of the priority screen. The existing `src/cloud_inputs.py` link mutant now targets `link_dir`, which falls back to a directory junction on Windows.

## Test gaps the check has found

The check has found test gaps five times, and each was closed:

- the first test set let 3 of 13 bugs through;
- the anomaly rules let 3 of 18 through;
- the tests for this round's modules let 2 of 51 through: the AM window within a morning, and the cache's link alignment. A third mutant looped forever against the paging test double, which now ends.
- the first Bus Arrival tests let 1 of 23 through: the tie rule in the list alignment. A fixture with a tie now pins it.
- the morning-loop tests let 1 of 12 through: a manual run's first pull must ignore a pull made just before it.
