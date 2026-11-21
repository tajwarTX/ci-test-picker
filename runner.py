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
    if SRC not in sys.path:
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


# ---------------------------------------------------------------- fork server
# A long-lived process that imported the PRISTINE sqlglot (and the test modules) once.
# For a change that only edits plain function/method bodies, each request forks a child
# that swaps those functions' code objects for the patched ones and runs the tests;
# anything it cannot swap exactly is reported as "noswap" (the caller then uses a fresh
# process on the patched tree instead).

import ast as _ast
import types as _types


def _code_by_qualname(code, out):
    for c in code.co_consts:
        if isinstance(c, _types.CodeType):
            out.setdefault(c.co_qualname, []).append(c)
            _code_by_qualname(c, out)
    return out


def _swap(rel, new_text, changed_qualnames):
    """Swap the code of each changed function in the already-imported module."""
    modname = rel[:-3].replace("/", ".").removesuffix(".__init__")
    mod = sys.modules.get(modname) or importlib.import_module(modname)
    new_codes = _code_by_qualname(compile(new_text, mod.__file__, "exec", dont_inherit=True), {})
    for qn in changed_qualnames:
        cands = new_codes.get(qn, [])
        if len(cands) != 1:
            return False
        obj = mod
        parts = qn.split(".")
        for i, part in enumerate(parts):
            holder = obj
            obj = obj.__dict__.get(part) if isinstance(obj, type) else getattr(obj, part, None)
            if obj is None:
                return False
            if i < len(parts) - 1 and not isinstance(obj, type):
                return False
        if isinstance(obj, (staticmethod, classmethod)):
            obj = obj.__func__
        if type(obj) is not _types.FunctionType or hasattr(obj, "__wrapped__"):
            return False
        if obj.__code__.co_qualname != qn or obj.__code__.co_freevars != cands[0].co_freevars:
            return False
        obj.__code__ = cands[0]
    return True


def serve():
    os.environ["SKIP_INTEGRATION"] = "0"
    cf.ProcessPoolExecutor = SyncExecutor
    import concurrent.futures.process as cfp
    cfp.ProcessPoolExecutor = SyncExecutor
    sys.path.append(SRC)
    import sqlglot  # noqa: F401  (pristine)
    for line in sys.stdin:
        req = json.loads(line)
        for tid in req["ids"]:  # import test modules in the parent, once (pristine)
            try:
                importlib.import_module(tid.split("::")[0][:-3].replace("/", "."))
            except BaseException:  # noqa: BLE001
                pass
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            out = os.fdopen(w, "w")
            try:
                ok = all(_swap(rel, d["text"], d["functions"]) for rel, d in req["files"].items())
                if not ok:
                    out.write(json.dumps({"noswap": True}))
                else:
                    import io, contextlib
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        main(req["ids"])
                    out.write(buf.getvalue().strip().splitlines()[-1])
            except BaseException:  # noqa: BLE001
                out.write(json.dumps({"error": True}))
            out.flush()
            os._exit(0)
        os.close(w)
        data = os.fdopen(r).read()
        os.waitpid(pid, 0)
        sys.stdout.write((data.strip() or json.dumps({"error": True})) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--serve"]:
        serve()
        sys.exit(0)
    sys.exit(main(sys.argv[1:]))
