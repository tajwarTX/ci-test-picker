"""data/max_map.json.gz for the test_max_nodes check (run in the mapper image; local only).
  python build_max_map.py MAXCOV_DIR ISO_DIR OUT
  huge:       lines the two huge parses run (fresh process)  -> a change there: no check
  unmodeled:  lines the real test ran (isolated coverage, incl. imports) that neither the
              huge parses nor the four cheap checks explain   -> a change there: no check"""
import glob, gzip, json, os, sqlite3, sys
from coverage.numbits import numbits_to_nums
cov_dir, iso_dir, out = sys.argv[1:4]
T = "tests/test_parser.py::TestParser::test_max_nodes"
huge = {r: set(v) for r, v in json.load(open(os.path.join(cov_dir, "huge.json"))).items()}
cheap = {r: set(v) for r, v in json.load(open(os.path.join(cov_dir, "cheap.json"))).items()}
real = {}
for path in glob.glob(os.path.join(iso_dir, ".coverage*")):
    con = sqlite3.connect(path)
    ctx = {cid for cid, c in con.execute("select id, context from context") if c and c.removeprefix("C|") == T}
    if not ctx:
        continue
    fname = dict(con.execute("select id, path from file"))
    for fid, cid, bits in con.execute("select file_id, context_id, numbits from line_bits"):
        if cid in ctx:
            rel = os.path.relpath(fname[fid], "/src")
            if rel.startswith("sqlglot/"):
                real.setdefault(rel, set()).update(numbits_to_nums(bits))
if not real:
    sys.exit("REFUSING: no isolated coverage for test_max_nodes")
unmodeled = {r: sorted(v - huge.get(r, set()) - cheap.get(r, set())) for r, v in real.items()}
unmodeled = {r: v for r, v in unmodeled.items() if v}
with gzip.open(out, "wt") as f:
    json.dump({"huge": {r: sorted(v) for r, v in huge.items()}, "unmodeled": unmodeled}, f, separators=(",", ":"))
print(f"real lines {sum(len(v) for v in real.values())}, huge {sum(len(v) for v in huge.values())}, "
      f"cheap {sum(len(v) for v in cheap.values())}, unmodeled {sum(len(v) for v in unmodeled.values())} "
      f"(import-time and other lines: a change there is never checked this way)")
