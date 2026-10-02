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
]


class HarnessError(RuntimeError):
    pass


def preflight():
    """Validate every target before any write; retain exact bytes for restoration."""
    originals = {name: (SRC / name).read_bytes() for name, _, _ in MUTANTS}
    for name, old, new in MUTANTS:
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
                              text=True, encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise HarnessError("child process timed out; this is not a test kill") from exc


def diagnostics(result):
    return (result.stdout + "\n" + result.stderr).strip()[-3500:]


def pytest_result(directory, env, *, stop_early):
    report = directory / "pytest.xml"
    args = ["-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}"]
    if stop_early:
        args.append("-x")
    result = run_child([*args, str(TESTS)], env)
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
        for index, (name, old, new) in enumerate(MUTANTS, 1):
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
                failed, _, result = pytest_result(case, env, stop_early=True)
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
