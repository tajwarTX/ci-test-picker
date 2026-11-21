"""Plan a safety check: tests the NEW map/selector drops compared with the OLD one.
  python3 plan_dropped.py OLD_MAP NEW_MAP COMMITS_DIR OUT_PLAN [MAX_SECONDS]
Every dropped test (up to MAX_SECONDS recorded, default: all) is listed per commit."""
import gzip, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import selector as S  # noqa: E402
S.SRC = Path.home() / "buildathon/sqlglot-src"
tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
secs = {t["number"]: t["seconds"] for t in tests}
ident = {t["number"]: t["id"] for t in tests}
old = S.Selector(tests, json.load(gzip.open(sys.argv[1], "rt")), import_check=lambda p: True)
new = S.Selector(tests, json.load(gzip.open(sys.argv[2], "rt")), import_check=lambda p: True)
limit = float(sys.argv[5]) if len(sys.argv) > 5 else 1e9
plan, total, tpc = {}, 0, 0
for p in sorted(Path(sys.argv[3]).glob("*.patch")):
    text = p.read_text()
    a, _ = old.select(text)
    b, why = new.select(text)
    left = sorted(n for n in a - b if secs[n] <= limit)
    if left:
        plan[p.stem] = {"patch": text, "run": [ident[n] for n in left]}
        total += len(left)
        tpc += any(ident[n].endswith("test_execute_tpcds") for n in left)
json.dump(plan, open(sys.argv[4], "w"))
print(f"{len(plan)} commits drop tests; {total} dropped tests to run; TPC-DS dropped in {tpc} commits")
