"""Per-statement coverage of long unittest methods (mapper only).

Uses runner.py's own droppable_statements / sliced_method (repo mounted at /blast), so the
map is built with exactly the slicing the runtime does. The parent imports sqlglot and the
test module once (process pools replaced by runner.SyncExecutor, as runner.main does).
From that state it forks one child per run, cold:
  "pre"  = the method sliced with no droppable statement kept,
  stmt i = the method sliced keeping only droppable statement i (key: its first line in
           the test FILE),
each with coverage on (source=/src/sqlglot); CPU is timed in separate children with
coverage off (cpu_i = child - pre, floor 0). Sanity: pre passes, every single-statement
child passes, and the full sliced method (all kept) passes.

  python stmt_cov.py OUT TEST_ID count          -> prints the number of droppable stmts
                                                    (or SKIP <reason>)
  python stmt_cov.py OUT TEST_ID LO HI           -> one shard: stmts[LO:HI]
Writes OUT/<test_id with / and :: -> _ and __>.<LO>.json =
  {"test", "lo", "hi", "stmts": [line..], "pre": {rel: [lines]}, "pre_cpu", "cpu": [..],
   "stmt_lines": [{rel: [lines]}, ...], "ok": {...}, "full_cpu"}
"""
import concurrent.futures as cf
import concurrent.futures.process as cfp
import importlib
import inspect
import json
import os
import sys
import textwrap
import time
import unittest
import ast

sys.path.insert(0, "/src")
sys.path.insert(0, "/blast")
os.environ["SKIP_INTEGRATION"] = "0"
import coverage  # noqa: E402
import runner  # noqa: E402

cf.ProcessPoolExecutor = runner.SyncExecutor
cfp.ProcessPoolExecutor = runner.SyncExecutor

out, tid = sys.argv[1], sys.argv[2]
path, cls_name, meth = tid.split("::")
mod = importlib.import_module(path[:-3].replace("/", "."))
klass = getattr(mod, cls_name)
orig = klass.__dict__.get(meth)


def stmt_lines():
    """First lines (in the test file) of the droppable statements, computed the way
    runner.sliced_method numbers them."""
    lines, first = inspect.getsourcelines(orig)
    tree = ast.parse(textwrap.dedent("".join(lines)))
    fn = tree.body[0]
    ast.increment_lineno(tree, first - 1)
    return [st.lineno for st in runner.droppable_statements(fn)]


def skip(reason):
    print("SKIP", reason)
    sys.exit(0)


if orig is None:
    skip("method not defined on the class itself")
try:
    if runner.sliced_method(klass, meth, set()) is None:
        skip("sliced_method is None")
except SystemExit:
    raise
except Exception as e:  # noqa: BLE001
    skip(f"sliced_method raises {type(e).__name__}")
try:
    stmts = stmt_lines()
except Exception as e:  # noqa: BLE001
    skip(f"source: {e!r}")
if len(set(stmts)) != len(stmts):
    skip("two droppable statements share a first line")
if len(stmts) < 5:
    skip(f"only {len(stmts)} droppable statements")


def run(keep):
    new = runner.sliced_method(klass, meth, keep)
    setattr(klass, meth, new)
    res = unittest.TestResult()
    unittest.TestSuite([klass(meth)]).run(res)
    return not (res.failures or res.errors or res.unexpectedSuccesses or res.skipped)


def child(keep, cov):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        c = coverage.Coverage(source=["/src/sqlglot"], data_file=None) if cov else None
        if c:
            c.start()
        t0 = time.process_time()
        try:
            ok = run(keep)
        except BaseException:  # noqa: BLE001
            ok = False
        cpu = time.process_time() - t0
        lines = {}
        if c:
            c.stop()
            d = c.get_data()
            lines = {os.path.relpath(f, "/src"): sorted(d.lines(f) or []) for f in d.measured_files()}
        with os.fdopen(w, "w") as fh:
            json.dump({"ok": ok, "lines": lines, "cpu": cpu}, fh)
        os._exit(0)
    os.close(w)
    raw = os.fdopen(r).read()
    os.waitpid(pid, 0)
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {"ok": False, "lines": {}, "cpu": 0.0}


if sys.argv[3:4] == ["count"]:
    print(len(stmts))
    sys.exit(0)
lo, hi = int(sys.argv[3]), int(sys.argv[4])
pre = child(set(), True)
pre_t = child(set(), False)
full = child(set(stmts), False)
ok = {"pre": pre["ok"] and pre_t["ok"], "full": full["ok"], "stmts": True}
stmt_cov, cpu = [], []
for line in stmts[lo:hi]:
    d = child({line}, True)
    t = child({line}, False)
    ok["stmts"] = ok["stmts"] and d["ok"] and t["ok"]
    stmt_cov.append(d["lines"])
    cpu.append(round(max(0.0, t["cpu"] - pre_t["cpu"]), 5))
json.dump({"test": tid, "lo": lo, "hi": hi, "stmts": stmts, "pre": pre["lines"],
           "pre_cpu": round(pre_t["cpu"], 5), "cpu": cpu, "stmt_lines": stmt_cov, "ok": ok,
           "full_cpu": round(full["cpu"], 5)},
          open(os.path.join(out, tid.replace("/", "_").replace("::", "__") + f".{lo:05d}.json"), "w"))
print(tid, lo, hi, "ok", ok, "pre_cpu", round(pre_t["cpu"], 3), "stmt_cpu", round(sum(cpu), 3),
      "full_cpu", round(full["cpu"], 3), flush=True)
