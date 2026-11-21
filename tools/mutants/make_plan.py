"""Build plan.json for check_excluded.py: for each synthetic commit where the selector
NARROWED (class / setting / module-constant rules), list the cheap tests it left out
of the broad 'whole file' set.  python3 make_plan.py MAP COMMITS_DIR OUT MAX_SECONDS"""
import gzip, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import selector as S  # noqa: E402

S.SRC = Path.home() / "buildathon/sqlglot-src"
tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
secs = {t["number"]: t["seconds"] for t in tests}
ident = {t["number"]: t["id"] for t in tests}
mp = json.load(gzip.open(sys.argv[1], "rt"))
sel = S.Selector(tests, mp, import_check=lambda p: True)
limit = float(sys.argv[4])
plan = {}
for p in sorted(Path(sys.argv[2]).glob("*.patch")):
    text = p.read_text()
    chosen, why = sel.select(text)
    if not any(k in " ".join(why) for k in ("class 1", "class 2", "setting 1", "setting 2", "entry 1", "entry 2")):
        continue
    broad = set()
    for ch in S.parse_patch(text):
        if ch.old and ch.old.endswith(".py"):
            broad |= sel.whole_file(ch.old)
    left_out = sorted(n for n in broad - chosen if secs[n] <= limit)
    plan[p.stem] = {"patch": text, "run": [ident[n] for n in left_out], "chosen": len(chosen)}
json.dump(plan, open(sys.argv[3], "w"))
print(f"{len(plan)} narrowed commits; {sum(len(v['run']) for v in plan.values())} excluded cheap tests to run")
