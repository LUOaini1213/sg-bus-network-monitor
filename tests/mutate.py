"""Mutation check for the unit tests: each mutant must make at least one test fail."""
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
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
]

escaped = 0
for fname, old, new in MUTANTS:
    f = SRC / fname
    text = f.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"mutation target not unique in {fname}: {old}"
    f.write_text(text.replace(old, new), encoding="utf-8")
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", str(SRC.parent / "tests" / "test_core.py")],
                           capture_output=True, text=True)
    finally:
        f.write_text(text, encoding="utf-8")
    killed = r.returncode != 0
    escaped += not killed
    print(f"{'killed ' if killed else 'ESCAPED'}  {fname}: {old[:60]}")
print(f"{len(MUTANTS) - escaped}/{len(MUTANTS)} mutants killed")
sys.exit(1 if escaped else 0)
