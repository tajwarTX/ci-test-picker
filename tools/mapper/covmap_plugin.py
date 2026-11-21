"""pytest plugin used ONLY to build the coverage map (not part of the submission's runtime).

Two modes:
  * single-process (default): one context per test; import-time lines land in "".
  * isolated (MAP_TEST=<nodeid>): pytest runs one test in a fresh process. Lines run
    while collecting/importing go to context "C|<nodeid>", lines run by the test go
    to "<nodeid>". So each test is credited with every module it loads.

For each test it also records which non-Python files under the source tree it
opened or listed (fixtures).
"""

import json
import os
import sys

import coverage

SRC = os.environ.get("MAP_SRC", "/src")
OUT = os.environ.get("MAP_OUT", "/out")
ISOLATED = os.environ.get("MAP_TEST")
BASE = f"C|{ISOLATED}" if ISOLATED else ""

_cov = None
_current = [BASE]
_opened: dict[str, set] = {}


def _audit(event, args):
    if event in ("open", "os.listdir", "os.scandir", "glob.glob"):
        try:
            path = args[0]
            if isinstance(path, int) or path is None:
                return
            path = os.path.abspath(os.fsdecode(path))
        except Exception:
            return
        if path.startswith(SRC + "/") and "__pycache__" not in path and not path.endswith((".py", ".pyc")):
            _opened.setdefault(_current[0], set()).add(os.path.relpath(path, SRC))


def pytest_configure(config):
    global _cov
    _cov = coverage.Coverage(
        data_file=os.path.join(OUT, ".coverage"),
        data_suffix=bool(ISOLATED),
        source=[os.path.join(SRC, "sqlglot"), os.path.join(SRC, "tests")],
        branch=False,
        concurrency=["thread"],
    )
    _cov.start()
    _cov.switch_context(BASE)
    sys.addaudithook(_audit)
    if ISOLATED:
        # Child processes (spawned pool workers) see this and record coverage
        # under MAP_TEST via child_cov.py.
        os.environ["MAP_PARENT_PID"] = str(os.getpid())


def pytest_runtest_logstart(nodeid, location):
    _current[0] = nodeid
    _cov.switch_context(nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _current[0] = BASE
    _cov.switch_context(BASE)


def pytest_unconfigure(config):
    _cov.stop()
    _cov.save()
    name = "opened.json"
    if ISOLATED:
        name = f"opened.{os.getpid()}.{__import__('time').time_ns()}.json"
    with open(os.path.join(OUT, name), "w") as f:
        json.dump({k: sorted(v) for k, v in _opened.items()}, f)
