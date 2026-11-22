"""Per-case coverage of tests that loop over SQL fixture files (mapper only).

The parent imports sqlglot and the test module once (process pools replaced by the
in-process executor runner.py uses at run time). For the setup ("pre": no case) and then
for each fixture case it forks a child that runs the test with the fixture loaders
(load_sql_fixture_pairs and load_sql_fixtures, patched in tests.helpers and in every
loaded tests.* module) letting through only that one case, with coverage on. Each child
starts from the same just-imported state, so a case's lines include everything it
computes itself (cold). CPU is measured in separate children without coverage.

  python case_cov.py OUT TEST_ID [count | LO HI]   (LO HI: only cases[LO:HI], one shard)
Writes OUT/<test_id with :: -> __>.json =
  {"test", "pre": {rel: [lines]}, "pre_cpu", "cases": [[loader, file, idx], ...],
   "cpu": [...], "case_lines": [{rel: [lines]}, ...], "ok": {...}, "n_seen"}
"""
import concurrent.futures as cf
import concurrent.futures.process as cfp
import importlib
import json
import os
import pickle
import sys
import time
import unittest

sys.path.insert(0, "/src")
os.environ["SKIP_INTEGRATION"] = "0"
import coverage  # noqa: E402

out, tid = sys.argv[1], sys.argv[2]


class SyncExecutor(cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        f = cf.Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


cf.ProcessPoolExecutor = SyncExecutor
cfp.ProcessPoolExecutor = SyncExecutor

path, cls, meth = tid.split("::")
import tests.helpers as H  # noqa: E402

mod = importlib.import_module(path[:-3].replace("/", "."))
LOADERS = ("load_sql_fixture_pairs", "load_sql_fixtures")
REAL = {n: getattr(H, n) for n in LOADERS}
seen = []


def install(allow):
    def make(name):
        real = REAL[name]

        def gen(filename):
            for i, item in enumerate(real(filename)):
                key = (name, filename, i)
                seen.append(key)
                if allow is None or key in allow:
                    yield item
        return gen

    for name in LOADERS:
        f = make(name)
        for m in list(sys.modules.values()):
            mn = getattr(m, "__name__", "") or ""
            if (mn == "tests" or mn.startswith("tests.")) and getattr(m, name, None) is REAL[name]:
                setattr(m, name, f)


def run():
    res = unittest.TestResult()
    unittest.TestSuite([getattr(mod, cls)(meth)]).run(res)
    return not (res.failures or res.errors or res.unexpectedSuccesses or res.skipped)


def child(allow, cov):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        seen.clear()
        install(allow)
        c = coverage.Coverage(source=["/src/sqlglot"], data_file=None) if cov else None
        if c:
            c.start()
        t0 = time.process_time()
        ok = run()
        cpu = time.process_time() - t0
        lines = {}
        if c:
            c.stop()
            d = c.get_data()
            lines = {os.path.relpath(f, "/src"): sorted(d.lines(f) or []) for f in d.measured_files()}
        with os.fdopen(w, "w") as fh:
            json.dump({"ok": ok, "lines": lines, "seen": seen, "cpu": cpu}, fh)
        os._exit(0)
    os.close(w)
    data = json.loads(os.fdopen(r).read())
    os.waitpid(pid, 0)
    return data


pre = child(set(), True)
cases = [tuple(x) for x in pre["seen"]]
if sys.argv[3:4] == ["count"]:
    print(len(cases))
    sys.exit(0)
lo, hi = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (0, len(cases))
pre_t = child(set(), False)
whole = child(None, False)
ok = {"pre": pre["ok"] and pre_t["ok"], "all": whole["ok"], "cases": True}
case_lines, cpu = [], []
for case in cases[lo:hi]:
    d = child({case}, True)
    t = child({case}, False)
    ok["cases"] = ok["cases"] and d["ok"] and t["ok"]
    case_lines.append(d["lines"])
    cpu.append(round(max(0.0, t["cpu"] - pre_t["cpu"]), 5))
json.dump({"test": tid, "lo": lo, "hi": hi, "pre": pre["lines"], "pre_cpu": round(pre_t["cpu"], 5), "cases": cases, "cpu": cpu,
           "case_lines": case_lines, "ok": ok, "n_seen": len(whole["seen"]), "whole_cpu": round(whole["cpu"], 5)},
          open(os.path.join(out, tid.replace("/", "_").replace("::", "__") + f".{lo:05d}.json"), "w"))
print(tid, "cases", len(cases), "ok", ok, "n_seen", len(whole["seen"]), "pre_cpu", round(pre_t["cpu"], 3),
      "case_cpu", round(sum(cpu), 3), "whole_cpu", round(whole["cpu"], 3), flush=True)
