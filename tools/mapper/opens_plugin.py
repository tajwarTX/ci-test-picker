"""pytest plugin (mapper only): record every file a test opens, runs or lists in the
repository OUTSIDE the coverage-measured code (anything except sqlglot/**/*.py and
tests/**/*.py), e.g. sqlglotc/setup.py run by test_sqlglotc, or fixture data."""

import json
import os
import sys
import time

SRC = os.environ.get("MAP_SRC", "/src")
TEST = os.environ.get("MAP_TEST", "")
_seen: set = set()


def _audit(event, args):
    if event in ("open", "os.listdir", "os.scandir", "glob.glob", "compile"):
        try:
            path = args[1] if event == "compile" else args[0]
            if isinstance(path, int) or path is None or isinstance(path, (bytes,)) and not path:
                return
            if not isinstance(path, (str, bytes, os.PathLike)):
                return
            path = os.path.abspath(os.fsdecode(path))
        except Exception:
            return
        if not path.startswith(SRC + "/") or "__pycache__" in path or "/<" in path:
            return
        rel = os.path.relpath(path, SRC)
        if rel.endswith(".py") and rel.startswith(("sqlglot/", "tests/")):
            return  # measured by coverage
        _seen.add(rel)


def install():
    sys.addaudithook(_audit)


def dump():
    out = os.environ.get("MAP_OUT", "/out")
    with open(os.path.join(out, f"opened.{os.getpid()}.{time.time_ns()}.json"), "w") as f:
        json.dump({TEST: sorted(_seen)}, f)


def pytest_configure(config):
    install()
    os.environ["MAP_PARENT_PID"] = str(os.getpid())


def pytest_unconfigure(config):
    dump()
