"""CI check: the test count the README states is the number pytest collects.

    python tests/check_test_count.py      # exit 1 and the two numbers when they differ
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collected():
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"^(\d+) tests? collected", out.stdout, re.M)
    if out.returncode != 0 or not m:
        sys.exit(f"pytest could not collect the tests (exit {out.returncode}):\n{out.stdout[-2000:]}{out.stderr[-2000:]}")
    return int(m.group(1))


def stated():
    m = re.search(r"^  - ([\d,]+) tests, and `tests/mutate.py`", (ROOT / "README.md").read_text(encoding="utf-8"), re.M)
    if not m:
        sys.exit("README.md no longer states the test count in the Validation > Tests list")
    return int(m.group(1).replace(",", ""))


if __name__ == "__main__":
    have, say = collected(), stated()
    if have != say:
        sys.exit(f"README.md says {say} tests; pytest collects {have}")
    print(f"README test count matches: {have}")
