"""Each valid mutant must fail a test in the complete, initially passing suite.

Run from any directory: python tests/mutate.py. Importing this module does not
run tests or alter sources. Collection/setup errors are harness errors, not kills.
"""
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
# New mutants run only the test files that cover their module (a kill there is a kill in the full suite;
# an escape is reported even if another file might have caught it).
PD = ["test_priority_days.py"]
CO = ["test_collect.py"]
FC = ["test_forecast.py"]
SF = ["test_safety.py"]
FI = ["test_finance.py"]
BA = ["test_busarrival.py", "test_busarrival_analysis.py"]
MUTANTS = [
    ("corridors.py", "sum(nums[:2]) / len(nums[:2])", "max(nums[:2])"),
    ("corridors.py", "nums = [n for n in nums if n > 0]", "nums = nums"),
    ("demand.py", "d.weekday() < 5 and d not in HOLIDAYS_2026", "d.weekday() < 5"),
    ("demand.py", "dt.date(2026, 8, 10), ", ""),
    ("demand.py", "big & (s.robust_z >= z_cut)", "(s.robust_z >= z_cut)"),
    ("demand.py", "(s[base] >= min_daily) | (s[month] >= min_daily)", "(s[base] >= 0)"),
    ("demand.py", "mad = (lr - med).abs().median() * 1.4826", "mad = lr.std()"),
    ("demand.py", "robust_z=(lr - med) / mad", "robust_z=lr / mad"),
    ("priority.py", "np.arctan2(x1 - x0, y1 - y0)", "np.arctan2(y1 - y0, x1 - x0)"),
    ("priority.py", '"S\'GOON": "SERANGOON", ', ""),
    ("priority.py", "else (t.time() >= lo or t.time() <= hi)", "else (lo <= t.time() <= hi)"),
    ("priority.py", "d.MinimumSpeed + 5, (d.MinimumSpeed + d.MaximumSpeed + 1) / 2",
     "(d.MinimumSpeed + d.MaximumSpeed + 1) / 2, (d.MinimumSpeed + d.MaximumSpeed + 1) / 2"),
    ("priority.py", "(weekday_only and t.weekday() >= 5)", "(weekday_only and t.weekday() > 5)"),
    ("demand.py", "w[list(base)].median(axis=1, skipna=False)", "w[list(base)].max(axis=1, skipna=False)"),
    ("anomalies.py", 'if r.flag == "drop" and len(removed) > len(added):', 'if r.flag == "drop" and removed:'),
    ("anomalies.py", "if jun < 0.8 * feb and abs(aug / feb - 1) <= 0.2:", "if jun < 0.8 * feb:"),
    ("anomalies.py", "if min(jul, aug) >= 1.4 * max(feb, jun):", "if aug >= 1.4 * max(feb, jun):"),
    ("anomalies.py", "if r.near_campus and jun < 0.8 * aug and jul < 0.8 * aug:", "if r.near_campus:"),
    ("anomalies.py", "before = {RENUMBERED.get(s, s) for s in before}", "before = set(before)"),
    ("walk_network.py", "return tags.get(\"foot\") != \"no\"", "return True"),
    ("walk_network.py", "if tags.get(\"area\") == \"yes\" or ", "if "),
    # Review regressions: missing speeds must not act as zero-valued samples,
    # and every link needs its own valid sample floor before it is ranked.
    ("priority.py", "np.isfinite(x.kmh) & (x.kmh > 0) & ", ""),
    ("priority.py", "by_link.median().where(by_link.count() >= min_snapshots)", "by_link.median()"),
    ("priority.py", "ok = cl[valid].copy()", "ok = cl[cl.match_ratio >= 0.5].copy()"),
    ("priority.py", ".where(g.observed_m >= MIN_VALID_COVERAGE * g.link_km * 1000)", ""),
    # An empty current run must replace yesterday's ranking and its metadata.
    ("priority.py", "    out = Path(output_dir) if output_dir is not None else OUT",
     "    if ranked.empty:\n        return ranked, metadata\n    out = Path(output_dir) if output_dir is not None else OUT"),
    # Recreate dedup-before-filter: a normal July row hides a median-only flag.
    ("od.py", '''        source = source.loc[source.flag.isin(["surge", "drop"])].copy()
        source["flag_baseline"] = baseline
        candidates.append(source)
    # Filter before deduplication: a normal July row must not hide a flag from
    # the median baseline. If both flag, retain July's metadata as before.
    s = pd.concat(candidates, ignore_index=True).drop_duplicates("stop")''',
     '''        source = source.copy()
        source["flag_baseline"] = baseline
        candidates.append(source)
    s = pd.concat(candidates, ignore_index=True).drop_duplicates("stop")
    s = s.loc[s.flag.isin(["surge", "drop"])].copy()'''),
    ("od.py", 'source["flag_baseline"] = baseline', 'source["flag_baseline"] = "2026-07"'),
    # Duplicate sparse coordinates must represent alternative paths, not sums.
    ("coverage_walk.py", 'paths.groupby(["u", "v"], as_index=False)["length_m"].min()',
     'paths.groupby(["u", "v"], as_index=False)["length_m"].sum()'),
    ("coverage_walk.py", '"u": np.minimum(u, v), "v": np.maximum(u, v)', '"u": u, "v": v'),
    ("coverage_walk.py", '.groupby(level=0).min()', '.groupby(level=0).sum()'),
    # A larger parallel path listed first is not necessarily the shortest one.
    ("coverage_walk.py", 'paths.groupby(["u", "v"], as_index=False)["length_m"].min()',
     'paths.groupby(["u", "v"], as_index=False)["length_m"].first()'),
    # Sparse zero-weight access arcs are valid: an epsilon loses the 400 m edge.
    ("coverage_walk.py", 'snap_min.to_numpy()])', 'np.maximum(snap_min.to_numpy(), 1e-3)])'),
    # Multi-morning bus priority (priority_days.py).
    ("priority_days.py", "need = math.ceil(len(daily) / 2)", "need = 1", PD),
    ("priority_days.py", "med = nanmedian(stack)", "med = np.nanmean(stack, axis=0)", PD),
    ("priority_days.py", "(mins >= a) | (mins <= b)", "(mins >= a) & (mins <= b)", PD),
    ("priority_days.py", "if x.weekday() < 5 and x.date() not in holidays", "if x.date() not in holidays", PD),
    ("priority_days.py", "x.weekday() < 5 and x.date() not in holidays and ", "x.weekday() < 5 and ", PD),
    ("priority_days.py", "and n >= min_snapshots]", "and n >= 1]", PD),
    ("priority_days.py", "covered = (den > 0) & (den >= priority.MIN_VALID_COVERAGE * link_km * 1000)",
     "covered = den > 0", PD),
    ("priority_days.py", "return len(ta & tb) / k", "return len(ta & tb) / len(ta | tb)", PD),
    ("priority_days.py", "ta = set(a[common].sort_values(ascending=False).index[:k])",
     "ta = set(a[common].sort_values(ascending=True).index[:k])", PD),
    ("priority_days.py", "period_median(speeds[am & (dates == d)], min_snapshots)",
     "period_median(speeds[dates == d], min_snapshots)", PD),
    ("priority_days.py", "s.sort_values(ascending=False).index[:k])] += 1",
     "s.sort_values(ascending=False).index[:k + 1])] += 1", PD),
    # Collector and speed cache.
    ("collect.py", "if len(p) < PAGE:", "if not p:", CO),
    ("collect.py", 'if r.status_code >= 400 and "quota" in r.text.lower():', "if False:", CO),
    ("collect.py", "    os.replace(tmp, path)\n", "", CO),
    ("collect.py", "(int(epoch // every) + 1) * every", "round(epoch / every) * every", CO),
    # GitHub Actions collector: Singapore weekday mornings only, no public holidays, quota backoff.
    ("collect.py", "if t.date() in holidays:", "if False:", CO),
    ("collect.py", "t = now.astimezone(SGT).replace(tzinfo=None) if now.tzinfo else now", "t = now.replace(tzinfo=None)",
     CO),
    ("collect.py", "if t.weekday() >= 5:", "if t.weekday() > 5:", CO),
    ("collect.py", "if not lo <= t.time() <= end:", "if not lo <= t.time():", CO),
    ("collect.py", "if t.time() >= nlo or t.time() <= nhi:", "if nlo <= t.time() <= nhi:", CO),
    ("collect.py", "            time.sleep(wait)\n", "", CO),
    # Quota exhausted after the backoff is a warning and exit 0; three tries, not four.
    ("collect.py", "if isinstance(e, QuotaExceeded):", "if False:", CO),
    ("collect.py", "QUOTA_BACKOFF = (60, 120)", "QUOTA_BACKOFF = (60, 120, 240)", CO),
    # One looping job per morning: slots on the 15-minute grid, early and late starts, backups, stop at 10:00.
    ("collect.py", "k = math.ceil(max((now - start).total_seconds(), 0) / every)",
     "k = math.floor(max((now - start).total_seconds(), 0) / every)", CO),
    ("collect.py", "    while x <= until:", "    while x < until:", CO),
    ("collect.py", "(force or not near(x, step))", "(force or not near(x, step / 2))", CO),
    ("collect.py", "(force or not near(x, step))", "(not near(x, step))", CO),
    ("collect.py", "        if not near(x, step / 2):", "        if True:", CO),
    ("collect.py", "if x <= until + dt.timedelta(minutes=grace_min) and", "if x <= until and", CO),
    ("collect.py", "x = max(now.replace(second=0, microsecond=0), start)", "x = now.replace(second=0, microsecond=0)", CO),
    ("collect.py", "start - dt.timedelta(minutes=early_min) <= t < start", "False", CO),
    ("collect.py", "if ok and (t.time() >= nlo or t.time() <= nhi):", "if False:", CO),
    ("collect.py", 'if r.get("status", "").startswith("ok")]', 'if r.get("status", "")]', CO),
    ("collect.py", "while (left := (t - clock()).total_seconds()) > 0:",
     "while (left := (t - clock()).total_seconds()) > 600:", CO),
    ("collect.py", "t + dt.timedelta(seconds=every * (iterations - 1))", "t + dt.timedelta(seconds=every * iterations)",
     CO),
    ("speed_cache.py", "np.where(band >= 8, lo + 5, (lo + hi + 1) / 2)", "np.where(band >= 8, lo + 4.5, (lo + hi + 1) / 2)",
     CO),
    ("speed_cache.py", "out[pos[pos_ok]] = v[pos_ok]", "out[pos[pos_ok]] = v[:pos_ok.sum()]", CO),
    ("speed_cache.py", 'if str(z["signature"]) == sig:', "if True:", CO),
    # Forecasting.
    ("forecast.py", "if off[i] <= max_offset_min and off[i] < best[pos[i]]:", "if off[i] <= max_offset_min:", FC),
    ("forecast.py", "present[t - lags + 1:t + 1].all()", "present[t]", FC),
    ("forecast.py", "    idx = idx[first == last]\n", "", FC),
    ("forecast.py", "before[-val_days:]", "before[:val_days]", FC),
    ("forecast.py", "table = np.where(np.isfinite(by_type), by_type, by_tod[None])", "table = by_tod[None] + 0 * by_type",
     FC),
    ("forecast.py", "ok = np.isfinite(true) & np.isfinite(pred)", "ok = np.isfinite(pred)", FC),
    ("forecast.py", '"mae_lo": float(np.percentile(maes, 2.5))', '"mae_lo": float(np.percentile(maes, 5))', FC),
    ("forecast.py", "(starts[:, :, None] + np.arange(block)[None, None])", "(starts[:, :, None] + 0 * np.arange(block)[None, None])",
     FC),
    ("forecast.py", "for a, b in ((src, dst), (dst, src)):", "for a, b in ((src, dst),):", FC),
    ("forecast.py", "if np.hypot(*(end[j] - start[i])) < tol_m:", "if False:", FC),
    ("forecast.py", 'cols[f"lag{k}"] = X[idx - k].ravel()', 'cols[f"lag{k}"] = X[idx + k].ravel()', FC),
    ("forecast_gnn.py", "out.append(last[i:i + batch, :, None] + d * scale)", "out.append(d * scale)", FC),
    ("forecast_gnn.py", "val = 1.0 / deg[src]", "val = np.ones(len(src), dtype=np.float32)", FC),
    ("forecast_gnn.py", "x = torch.sparse.mm(a, x) if a.is_sparse else a @ x", "x = x", FC),
    # Safety brief.
    ("safety.py", "stats.chi2.ppf(alpha / 2, 2 * k) / 2", "stats.chi2.ppf(alpha / 2, 2 * k + 2) / 2", SF),
    ("safety.py", "scale = e2 / e1", "scale = e1 / e2", SF),
    ("safety.py", 'first_seen=("polled_at", "min")', 'first_seen=("polled_at", "max")', SF),
    ("safety.py", "t = np.clip(((p - a) * ab).sum(1) / np.where(L2 > 0, L2, 1), 0, 1)",
     "t = ((p - a) * ab).sum(1) / np.where(L2 > 0, L2, 1)", SF),
    ("safety.py", "float(seg_len[on_bus].sum() / seg_len.sum())", "float(on_bus.mean())", SF),
    ("safety.py", "ok = np.isfinite(dist) & (dist <= SNAP_M)", "ok = np.isfinite(dist)", SF),
    # Finance model and workbook.
    ("finance.py", "idx = np.ceil(np.round(km - 3.2, 1) - 1e-9)", "idx = np.floor(np.round(km - 3.2, 1))", FI),
    ("finance.py", "_, _, cls, km = min(cand)", "_, _, cls, km = max(cand)", FI),
    ("finance.py", "    if last <= first:\n        last += 1440  # last bus after midnight\n", "", FI),
    ("finance.py", "d.weekday() < 5 and d not in HOLIDAYS_2026", "d.weekday() < 5", FI),
    ("finance_model.py", "comb = np.clip(np.ceil(np.round(km - 3.2, 1) - 1e-9), 0, N_BANDS - 1)",
     "comb = np.clip(np.floor(np.round(km - 3.2, 1)), 0, N_BANDS - 1)", FI),
    ("finance_model.py", 'senior * (c["senior"] + c["pwd"]) / c["senior"]', "senior", FI),
    ("finance_model.py", 'factor = 1 - a["transfer_share"] * rebate / mean_adult', 'factor = 1 - a["transfer_share"]', FI),
    ("finance_model.py", "level = full * (1 + 95 / 365 * step)", "level = full * (1 + step)", FI),
    ("finance_model.py", "more = float((rides * (adult < f)).sum() / rides.sum())",
     "more = float((rides * (adult > f)).sum() / rides.sum())", FI),
    ("finance_workbook.py", '"=1-transfer_share*rebate/mean_adult"', '"=1-transfer_share"', FI),
    ("finance_workbook.py", "{comb}+1))", "{comb}+2))", FI),
    ("finance_workbook.py", 'ws.cell(rr, 2, f"=H{rr - 1}*(1+95/365*{step})")', 'ws.cell(rr, 2, f"=H{rr - 1}*(1+{step})")', FI),
    ("finance_workbook.py", '"=fy25_fare*1000000+old_days/365*fre2025_inc*bus_share"',
     '"=fy25_fare*1000000+old_days/365*fre2025_inc"', FI),
    ("finance_workbook.py", "feeder_now = f\"({blend_feeder('now')})\" if i == 0 else \"0\"", 'feeder_now = "0"', FI),
    # Bus Arrival collector: paced calls, quota backoff, window.
    ("busarrival.py", "lines, gap, t0 = [], period / len(stops), clock()", "lines, gap, t0 = [], 0, clock()", BA),
    ("busarrival.py", "self.period = min(self.period * 2, self.max)", "self.period = self.period", BA),
    ("busarrival.py", "if r.status_code == 429 or (", "if (", BA),
    ("busarrival.py", "for s in stops[i + 1:]]", "for s in []]", BA),
    ("busarrival.py", "if t.date() in holidays:", "if False:", BA),
    ("busarrival.py", "if self.clean >= self.recover and self.period > self.base:", "if False:", BA),
    ("busarrival.py", "            sleep(pause)\n", "", BA),
    # Bus Arrival inference and metrics.
    ("busarrival_analysis.py", 'if trk["how"] == "active" and e["monitored"] == 1 and e["eta"] <= t:',
     'if trk["how"] == "active" and e["eta"] <= t:', BA),
    ("busarrival_analysis.py", 'if (trk["last"] - t).total_seconds() <= GONE_SLACK_S:', "if True:", BA),
    ("busarrival_analysis.py", "        cost += IMPLAUSIBLE_S * ", "        cost += 0 * ", BA),
    ("busarrival_analysis.py", "if pairs else NO_MATCH_S", "if pairs else 0", BA),
    ("busarrival_analysis.py", "if t_prev is not None and (t - t_prev).total_seconds() > MAX_GAP_S:", "if False:", BA),
    ("busarrival_analysis.py", '"error_s": (mid - eta).total_seconds()', '"error_s": (eta - mid).total_seconds()', BA),
    ("busarrival_analysis.py", 'mid = trk["lo"] + (trk["hi"] - trk["lo"]) / 2', 'mid = trk["hi"]', BA),
    ("busarrival_analysis.py", "pd.cut(p.horizon_min, BINS, right=False, labels=BIN_LABELS)",
     "pd.cut(p.horizon_min, BINS, right=True, labels=BIN_LABELS)", BA),
    ("busarrival_analysis.py", "sd_min=lambda s: s.std(ddof=1)", "sd_min=lambda s: s.std(ddof=0)", BA),
    ("busarrival_analysis.py", 'h["bunched"] = h.headway_min < fraction * h.reference_min',
     'h["bunched"] = h.headway_min <= fraction * h.reference_min', BA),
    ("busarrival_analysis.py", 'h["reference_min"] = sched.fillna(med)', 'h["reference_min"] = sched', BA),
    ("busarrival_analysis.py", "if 6 * 60 + 30 <= m <= 8 * 60 + 30:", "if 6 * 60 + 30 <= m < 8 * 60 + 30:", BA),
    ("busarrival_analysis.py", """            if kind == "break":
                last = None""", """            if kind == "break":
                pass""", BA),
    ("busarrival_analysis.py", "            for svc in seen:", "            for svc in listed:", BA),
    ("busarrival_analysis.py", "if best_cost is None or cost < best_cost:", "if best_cost is None or cost <= best_cost:", BA),
    ("busarrival_analysis.py", "r = r[r.n >= min_n].copy()", "r = r.copy()", BA),
    ("busarrival_analysis.py", 'if x.get("status") == "ok" and x.get("t") and not blank(x):',
     'if x.get("status") == "ok" and x.get("t"):', BA),
    ("busarrival_analysis.py", "                if eta > horizon_end:", "                if False:", BA),
]


class HarnessError(RuntimeError):
    pass


def preflight():
    """Validate every target before any write; retain exact bytes for restoration."""
    originals = {m[0]: (SRC / m[0]).read_bytes() for m in MUTANTS}
    for name, old, new, *_ in MUTANTS:
        # Replacement anchors use LF independently of the checkout's line endings.
        source = originals[name].decode("utf-8").replace("\r\n", "\n")
        if source.count(old) != 1 or old == new:
            raise HarnessError(f"target must occur exactly once and change {name}: {old[:70]}")
        try:
            compile(source.replace(old, new), str(SRC / name), "exec")
        except (SyntaxError, ValueError) as exc:
            raise HarnessError(f"invalid mutant syntax in {name}: {exc}") from exc
    return originals


def child_environment(cache):
    env = os.environ.copy()
    # -B alone still reads existing pyc files. A fresh prefix also isolates reads
    # from old source and same-second, same-size mutants on every platform.
    env.update(PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1", PYTHONPYCACHEPREFIX=str(cache),
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    return env


def run_child(args, env):
    try:
        return subprocess.run([sys.executable, "-B", *args], cwd=ROOT, env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired as exc:
        raise HarnessError("child process timed out; this is not a test kill") from exc


def diagnostics(result):
    return (result.stdout + "\n" + result.stderr).strip()[-3500:]


def pytest_result(directory, env, *, stop_early, files=None):
    report = directory / "pytest.xml"
    args = ["-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"]
    if stop_early:
        args.append("-x")
    targets = [str(TESTS / f) for f in files] if files else [str(TESTS)]
    result = run_child([*args, *targets], env)
    if result.returncode not in (0, 1) or not report.is_file():
        raise HarnessError(f"pytest infrastructure/collection failure (exit {result.returncode})\n{diagnostics(result)}")
    try:
        root = ET.parse(report).getroot()
        cases = list(root.iter("testcase"))
        executed = [case for case in cases if case.find("skipped") is None]
        failures = list(root.iter("failure"))
        errors = list(root.iter("error"))
        suite_errors = sum(int(suite.get("errors", "0")) for suite in root.iter("testsuite"))
    except (ET.ParseError, ValueError) as exc:
        raise HarnessError("pytest produced an invalid result report") from exc
    if not executed or errors or suite_errors or (result.returncode == 1) != bool(failures):
        raise HarnessError(f"pytest did not produce a clean test outcome\n{diagnostics(result)}")
    return bool(failures), len(executed), result


def main():
    originals = preflight()
    killed = escaped = 0
    with tempfile.TemporaryDirectory(prefix="sgbus-mutation-") as temporary:
        temp = Path(temporary)
        baseline = temp / "baseline"
        baseline.mkdir()
        failed, count, result = pytest_result(baseline, child_environment(baseline / "cache"), stop_early=False)
        if failed:
            raise HarnessError(f"unmodified full suite failed; no mutations were written\n{diagnostics(result)}")
        print(f"baseline: {count} tests passed", flush=True)
        for index, (name, old, new, *files) in enumerate(MUTANTS, 1):
            # Refuse to overwrite changes made after preflight (e.g. another reviewer).
            for filename, original in originals.items():
                if (SRC / filename).read_bytes() != original:
                    raise HarnessError(f"source changed during this run: {filename}")
            path = SRC / name
            original = originals[name]
            source = original.decode("utf-8").replace("\r\n", "\n")
            mutant = source.replace(old, new)
            if b"\r\n" in original:
                mutant = mutant.replace("\n", "\r\n")
            case = temp / f"mutant-{index}"
            case.mkdir()
            env = child_environment(case / "cache")
            try:
                path.write_bytes(mutant.encode("utf-8"))
                # This executes only the module's imports, not its __main__ pipeline.
                check = ("import importlib; from pathlib import Path; "
                         f"p = Path({str(path)!r}); compile(p.read_bytes(), str(p), 'exec'); "
                         f"m = importlib.import_module({path.stem!r}); "
                         "assert Path(m.__file__).resolve() == p.resolve()")
                imported = run_child(["-c", check], env)
                if imported.returncode != 0:
                    raise HarnessError(f"mutant failed compilation/import in {name}; not a kill\n{diagnostics(imported)}")
                failed, _, result = pytest_result(case, env, stop_early=True, files=files[0] if files else None)
            finally:
                path.write_bytes(original)
            killed += failed
            escaped += not failed
            label = " ".join(old.split())[:65]
            print(f"[{index}/{len(MUTANTS)}] {'killed' if failed else 'ESCAPED'} {name}: {label}", flush=True)
        print(f"{killed}/{len(MUTANTS)} mutants killed; {escaped} escaped", flush=True)
    return 1 if escaped else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HarnessError as exc:
        print(f"HARNESS ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
