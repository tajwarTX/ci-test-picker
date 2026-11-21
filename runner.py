"""Run some sqlglot tests on the sqlglot found first on PYTHONPATH (the patched copy) and
print which ones fail, the way the scorer counts failures: a failure, an error, a failing
subtest, an unexpected success, or a test file that cannot be imported.

  python runner.py TEST_ID [TEST_ID ...]   ->  stdout: {"failed": [...], "ran": N}
Anything unexpected exits non-zero (the selector then keeps the full list).
Process pools run in-process (arguments and results still go through pickle).
"""
import concurrent.futures as cf
import importlib
import json
import os
import pickle
import sys
import unittest

SRC = os.environ.get("TPC_SRC", "/app/src")


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


class Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.bad = set()

    def _id(self, test):
        return getattr(test, "_br_id", None) or getattr(getattr(test, "test_case", None), "_br_id", None)

    def addFailure(self, test, err):
        self.bad.add(self._id(test))

    def addError(self, test, err):
        self.bad.add(self._id(test) or "<class setup>")

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.bad.add(self._id(test))

    def addUnexpectedSuccess(self, test):
        self.bad.add(self._id(test))


def main(ids):
    os.environ["SKIP_INTEGRATION"] = "0"
    cf.ProcessPoolExecutor = SyncExecutor
    import concurrent.futures.process as cfp
    cfp.ProcessPoolExecutor = SyncExecutor
    sys.path.append(SRC)  # tests/ from the pristine checkout, AFTER the patched sqlglot
    failed, suites = set(), {}
    for tid in ids:
        path, cls, meth = tid.split("::")
        mod = path[:-3].replace("/", ".")
        try:
            m = importlib.import_module(mod)
            case = getattr(m, cls)(meth)
        except BaseException:  # noqa: BLE001  the file (or class) does not load: the test fails
            failed.add(tid)
            if os.environ.get("BR_STOP_ON_FAIL") == "1":
                break
            continue
        case._br_id = tid
        suites.setdefault((mod, cls), []).append(case)
    stop = os.environ.get("BR_STOP_ON_FAIL") == "1"
    for (mod, cls), cases in suites.items():
        if stop and failed:
            break
        res = Result()
        unittest.TestSuite(cases).run(res)
        if "<class setup>" in res.bad or None in res.bad:
            failed.update(c._br_id for c in cases)  # class setup failed: all of them
        failed.update(t for t in res.bad if t and t != "<class setup>")
    if stop and failed:
        print(json.dumps({"stopped": True, "ran": len(ids)}))
        return 0
    print(json.dumps({"failed": sorted(failed), "ran": len(ids)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
