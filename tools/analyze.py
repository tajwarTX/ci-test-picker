"""Per-commit breakdown of the selector on the sample set (runs on the laptop, no Docker).

  python3 tools/analyze.py [MAP] [SRC]
MAP defaults to data/map.json.gz, SRC to ~/buildathon/sqlglot-src (pristine checkout).
The real score always comes from tools/eval.sh; this is for understanding misses.
"""

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import selector as S  # noqa: E402

map_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data/map.json.gz"
S.SRC = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.home() / "buildathon/sqlglot-src"

tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
answers = json.load(open(ROOT / "sample/answers.json"))["commits"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
name = {t["number"]: t["id"] for t in tests}
# import check assumed to pass here (laptop has no patched tree); tools/eval.sh runs it for real
sel = S.Selector(tests, json.load(gzip.open(map_path, "rt")), import_check=lambda patched: True)

total = misses = 0
for patch in sorted((ROOT / "sample/commits").glob("*.patch")):
    cid = patch.stem
    chosen, why = sel.select(patch.read_text())
    workers = S.split(chosen, dur)
    cost = (max(sum(dur[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
    fail = set(answers[cid]["failing"])
    missed = fail - chosen
    total += cost + (300000 if missed else 0)
    misses += bool(missed)
    ideal = (max(dur[n] for n in fail) + 2000) if fail else 0  # rough lower bound
    print(f"{cid} {answers[cid]['kind']:8} chose {len(chosen):4} cost {cost/1000:6.1f}s "
          f"(ideal ~{ideal/1000:5.1f}) fail {len(fail):2} {'MISS ' + str(len(missed)) if missed else 'ok'}")
    for w in why:
        print("      ", w)
    for n in sorted(missed)[:5]:
        print("       missed:", name[n])
print(f"\nTOTAL simulated {total/1000:.1f} s, misses {misses} (30 sample commits; CPU not included)")
