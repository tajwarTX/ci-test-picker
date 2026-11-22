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
    def startTest(self, test):
        _CURRENT[0] = getattr(test, "_br_id", None)
        super().startTest(test)

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


_CASES: dict = {}  # test id -> set of allowed (loader, filename, idx); tests not listed run whole
_CURRENT = [None]


def _install_case_filter():
    """Wrap the fixture loaders (in tests.helpers and every test module that bound them) so
    that a test listed in _CASES only gets its allowed cases (key: loader name, file, index
    in that call's sequence, the same key the case map was built with)."""
    import tests.helpers as H
    for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
        real = getattr(H, name)
        if getattr(real, "_br_real", None) is not None:
            continue

        def make(real, name):
            def gen(filename, *a, **k):
                allow = _CASES.get(_CURRENT[0])
                for i, item in enumerate(real(filename, *a, **k)):
                    if allow is None or (name, filename, i) in allow:
                        yield item
            gen._br_real = real
            return gen

        wrapped = make(real, name)
        setattr(H, name, wrapped)
        for mname, m in list(sys.modules.items()):
            if (mname == "tests" or mname.startswith("tests.")) and getattr(m, name, None) is real:
                setattr(m, name, wrapped)


def main(ids, cases=None):
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
    if cases:
        _CASES.clear()
        _CASES.update({t: {tuple(c) for c in v} for t, v in cases.items()})
        _install_case_filter()
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


def _lazy_pandas():
    """Placeholders for pandas and pandas.testing in the fork-server parent: importing a
    test module that does `from pandas.testing import assert_frame_equal` no longer loads
    pandas (~0.7 s CPU). The first real use (calling assert_frame_equal, or any other
    attribute of either module) imports the real pandas, puts it in sys.modules and
    delegates, so behaviour is unchanged."""
    if "pandas" in sys.modules:
        return
    import types

    real = {}

    def load():
        if not real:
            for k in ("pandas", "pandas.testing"):
                if isinstance(sys.modules.get(k), _Lazy):
                    del sys.modules[k]
            import pandas
            import pandas.testing
            real["pandas"], real["pandas.testing"] = pandas, pandas.testing
        return real

    class _Lazy(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__") and name.endswith("__"):
                raise AttributeError(name)
            return getattr(load()[self.__name__], name)

    def assert_frame_equal(*a, **k):
        return load()["pandas.testing"].assert_frame_equal(*a, **k)

    pd, pdt = _Lazy("pandas"), _Lazy("pandas.testing")
    pd.__path__ = []
    pdt.assert_frame_equal = assert_frame_equal
    pd.testing = pdt
    sys.modules["pandas"], sys.modules["pandas.testing"] = pd, pdt


def serve():
    os.environ["SKIP_INTEGRATION"] = "0"
    _lazy_pandas()
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
                if req.get("cpu_limit"):
                    import resource
                    lim = int(req["cpu_limit"]) + int(resource.getrusage(resource.RUSAGE_SELF).ru_utime) + 1
                    resource.setrlimit(resource.RLIMIT_CPU, (lim, lim + 1))
                ok = all(_swap(rel, d["text"], d["functions"]) for rel, d in req["files"].items())
                if not ok:
                    out.write(json.dumps({"noswap": True}))
                else:
                    import io, contextlib
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        if req.get("nostop"):
                            os.environ["BR_STOP_ON_FAIL"] = "0"
                        main(req["ids"], req.get("cases"))
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
