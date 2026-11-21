"""Record which sqlglot/test classes a process USES (mapper only, never in the submission).

A class counts as used when any Python method runs with `self` (an instance of it)
or `cls` (it) bound to it or to a subclass. Each used type's whole MRO is recorded,
so a change to a base class's settings reaches every test using a subclass.
Written at exit to $MAP_OUT/usage.<pid>.json as {test_id: ["module:Qual.Name", ...]}.
"""

import json
import os
import sys
import threading

_seen: set = set()
# Calls Python makes while CREATING classes (import time), not real use of them.
_CREATION = {"__init_subclass__", "__set_name__", "wrap", "trait"}


def _prof(frame, event, arg):
    if event != "call":
        return
    co = frame.f_code
    if not co.co_argcount:
        return
    name = co.co_varnames[0]
    if name != "self" and name != "cls":
        return
    if co.co_name in _CREATION:
        return
    try:
        obj = frame.f_locals[name]
    except Exception:
        return
    t = obj if isinstance(obj, type) else type(obj)
    if t not in _seen:
        _seen.add(t)


def install():
    sys.setprofile(_prof)
    threading.setprofile(_prof)


def dump(test_id):
    keys = set()
    for t in list(_seen):
        try:
            mro = t.__mro__
        except Exception:
            continue
        for k in mro:
            mod = getattr(k, "__module__", "") or ""
            if mod.startswith(("sqlglot", "tests")):
                keys.add(f"{mod}:{k.__qualname__}")
    out = os.environ.get("MAP_OUT", "/out")
    with open(os.path.join(out, f"usage.{os.getpid()}.json"), "w") as f:
        json.dump({test_id: sorted(keys)}, f)
