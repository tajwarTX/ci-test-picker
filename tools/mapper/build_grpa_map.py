"""data/grpa_map.json.gz: per whole-test check, the lines the REAL test runs (isolated
coverage, all processes) that the check does not run. A change touching one of them is
never cleared by that check.   python build_grpa_map.py COV_DIR ISO_DIR OUT"""
import glob, gzip, json, os, sqlite3, sys
from coverage.numbits import numbits_to_nums
cov_dir, iso_dir, out = sys.argv[1:4]
TESTS = {"tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch_exec",
         "tests/test_executor.py::TestExecutor::test_optimized_tpch": "tpch_opt",
         "tests/test_generator.py::TestGenerator::test_generator_memory": "genmem"}
real = {t: {} for t in TESTS}
for path in glob.glob(os.path.join(iso_dir, ".coverage*")):
    con = sqlite3.connect(path)
    ctx = {cid: c.removeprefix("C|") for cid, c in con.execute("select id, context from context") if c and c.removeprefix("C|") in TESTS}
    if not ctx:
        continue
    fname = dict(con.execute("select id, path from file"))
    for fid, cid, bits in con.execute("select file_id, context_id, numbits from line_bits"):
        if cid in ctx:
            rel = os.path.relpath(fname[fid], "/src")
            if rel.startswith("sqlglot/"):
                real[ctx[cid]].setdefault(rel, set()).update(numbits_to_nums(bits))
res = {}
for t, mode in TESTS.items():
    if not real[t]:
        sys.exit(f"REFUSING: no isolated coverage for {t}")
    probe = {r: set(v) for r, v in json.load(open(os.path.join(cov_dir, f"probe_{mode}.json"))).items()}
    unm = {r: sorted(v - probe.get(r, set())) for r, v in real[t].items()}
    res[t] = {r: v for r, v in unm.items() if v}
    print(t.split("::")[-1], "real", sum(len(v) for v in real[t].values()), "probe", sum(len(v) for v in probe.values()),
          "unmodeled", sum(len(v) for v in res[t].values()))
with gzip.open(out, "wt") as f:
    json.dump(res, f, separators=(",", ":"))
