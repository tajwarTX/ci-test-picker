"""Build data/tpc_map.json.gz for the TPC-DS probe (laptop; local only).

  python3 build_tpc_map.py TPCCOV_DIR ISO_DIR TESTS_JSON OUT

  base:       {rel: [lines]}  run by imports + table setup (a change there: no probe)
  q:          {rel: [[line, query bitmask], ...]}  lines each query's path runs
  unmodeled:  {rel: [lines]}  lines the real test_execute_tpcds ran (isolated coverage of
              that test, all its processes) that neither setup nor any query explains:
              a change touching one is never probed
"""
import glob, gzip, json, os, sqlite3, sys
from coverage.numbits import numbits_to_nums

cov_dir, iso_dir, tests_json, out = sys.argv[1:5]
TPC = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
base = {rel: set(ls) for rel, ls in json.load(open(os.path.join(cov_dir, "baseline.json"))).items()}
q = {}
n = 0
for f in sorted(glob.glob(os.path.join(cov_dir, "q*.json"))):
    i = int(os.path.basename(f)[1:4])
    n += 1
    for rel, ls in json.load(open(f)).items():
        d = q.setdefault(rel, {})
        for l in ls:
            d[l] = d.get(l, 0) | (1 << i)
if n != 99:
    sys.exit(f"REFUSING: {n} query coverage files, expected 99")
real = {}
for path in glob.glob(os.path.join(iso_dir, ".coverage*")):
    con = sqlite3.connect(path)
    ctx = {cid for cid, c in con.execute("select id, context from context") if c and c.removeprefix("C|") == TPC}
    if not ctx:
        continue
    fname = dict(con.execute("select id, path from file"))
    for fid, cid, bits in con.execute("select file_id, context_id, numbits from line_bits"):
        if cid in ctx:
            rel = os.path.relpath(fname[fid], "/src")
            if rel.startswith("sqlglot/"):
                real.setdefault(rel, set()).update(numbits_to_nums(bits))
if not real:
    sys.exit("REFUSING: no isolated coverage found for test_execute_tpcds")
unmodeled = {}
for rel, ls in real.items():
    extra = ls - base.get(rel, set()) - set(q.get(rel, {}))
    if extra:
        unmodeled[rel] = sorted(extra)
res = {"base": {r: sorted(v) for r, v in base.items()},
       "q": {r: sorted([l, m] for l, m in d.items()) for r, d in q.items()},
       "unmodeled": unmodeled}
with gzip.open(out, "wt") as f:
    json.dump(res, f, separators=(",", ":"))
tot_real = sum(len(v) for v in real.values())
print(f"queries {n}; real TPC-DS lines {tot_real}; unmodeled {sum(len(v) for v in unmodeled.values())} "
      f"in {len(unmodeled)} files; size {os.path.getsize(out)/1e6:.2f} MB")
