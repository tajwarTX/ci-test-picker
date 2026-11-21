"""Lines each whole-test check runs (fresh process, coverage from before any import).
  python grpa_cov.py OUT tpch_exec|tpch_opt|genmem      (OUT also holds the TPC data)"""
import json, os, sys
import coverage
out, mode = sys.argv[1], sys.argv[2]
os.environ.setdefault("TPC_SRC", "/src")
sys.path.insert(0, "/blast")
cov = coverage.Coverage(source=["/src/sqlglot"], data_file=None)
cov.start()
import tpc_probe  # noqa: E402
if mode == "genmem":
    assert tpc_probe.generator_memory() == 0
else:
    tpc_probe.tpch_digests(out)
cov.stop()
d = cov.get_data()
json.dump({os.path.relpath(f, "/src"): sorted(d.lines(f) or []) for f in d.measured_files()},
          open(os.path.join(out, f"probe_{mode}.json"), "w"))
