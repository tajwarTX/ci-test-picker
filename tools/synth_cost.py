"""Rank selection cost on unlabeled synthetic commits (no truth: shows where we spend).
  python3 tools/synth_cost.py MAP COMMITS_DIR [TOP]"""
import collections, gzip, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import selector as S
S.SRC = Path.home() / "buildathon/sqlglot-src"
tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
sel = S.Selector(tests, json.load(gzip.open(sys.argv[1], "rt")), import_check=lambda p: True)
rows = []
for p in sorted(Path(sys.argv[2]).glob("*.patch")):
    ch, why = sel.select(p.read_text())
    w = S.split(ch, dur)
    cost = (max(sum(dur[n] for n in x) for x in w) + 2000 * len(w)) if w else 0
    rows.append((cost / 1000, p.stem, len(ch), " ; ".join(why)[:140]))
rows.sort(reverse=True)
print(f"avg {sum(r[0] for r in rows)/len(rows):.2f}s over {len(rows)}")
by = collections.defaultdict(list)
for r in rows: by[r[1][-1]].append(r[0])
print({k: (len(v), round(sum(v) / len(v), 2)) for k, v in sorted(by.items())})
for r in rows[: int(sys.argv[3]) if len(sys.argv) > 3 else 15]:
    print(f"{r[0]:6.1f}s {r[1]} n={r[2]:4} {r[3]}")
