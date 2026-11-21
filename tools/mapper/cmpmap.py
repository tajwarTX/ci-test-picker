"""Comparison-operator profile (mapper only, never in the submission).

While mapping, sqlglot is loaded through an import hook that rewrites every comparison
with a single operator among  is / is not / == / != / < / <= / > / >=  into a call that
computes the original result exactly as before, and also evaluates the partner
operators on the SAME operand values:

    is <-> ==      is not <-> !=      < <-> <= <-> > <-> >=

For each comparison site and each partner operator it records whether the two results
ever differed in this test process (different value, different type, a non-bool result,
or an exception from the partner). A test in which they never differed runs exactly the
same way if that one operator is swapped: every comparison it evaluates gives the same
answer, so it takes the same path.

Written at exit to $MAP_OUT/cmp.<pid>.<ns>.json as
    {"test": id, "hit": [site...], "differ": {site: [partner ops]}}
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys
import threading
import time

SRC = os.environ.get("MAP_SRC", "/src")
TEST = os.environ.get("MAP_TEST", "")

OPS = {
    "Is": operator.is_, "IsNot": operator.is_not, "Eq": operator.eq, "NotEq": operator.ne,
    "Lt": operator.lt, "LtE": operator.le, "Gt": operator.gt, "GtE": operator.ge,
}
PARTNERS = {
    "Is": ("Eq",), "Eq": ("Is",), "IsNot": ("NotEq",), "NotEq": ("IsNot",),
    "Lt": ("LtE", "Gt", "GtE"), "LtE": ("Lt", "Gt", "GtE"),
    "Gt": ("GtE", "Lt", "LtE"), "GtE": ("Gt", "Lt", "LtE"),
}

_hit: set = set()
_differ: dict = {}
_local = threading.local()  # per-thread "probing a partner operator" flag


def __cmp__(site, op, left, right):
    result = OPS[op](left, right)  # the original comparison, unchanged
    if getattr(_local, "busy", False):
        return result
    _hit.add(site)
    done = _differ.get(site)
    partners = PARTNERS[op]
    if done is not None and len(done) == len(partners):
        return result
    _local.busy = True  # comparisons run by __eq__ & co. while probing are not recorded
    try:
        for p in partners:
            if done is not None and p in done:
                continue
            try:
                other = OPS[p](left, right)
                same = type(result) is bool and type(other) is bool and result is other
            except Exception:
                same = False
            if not same:
                _differ.setdefault(site, set()).add(p)
                done = _differ[site]
    finally:
        _local.busy = False
    return result


builtins.__cmp__ = __cmp__


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        if op not in OPS:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        return ast.copy_location(ast.Call(ast.Name("__cmp__", ast.Load()),
                                          [ast.Constant(site), ast.Constant(op), node.left, node.comparators[0]], []),
                                 node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        tree = ast.parse(self.get_data(path), path)
        tree = _Rewriter(os.path.relpath(path, SRC)).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        if not spec.origin or not spec.origin.startswith(SRC + "/sqlglot/"):
            return spec
        spec.loader = _Loader(spec.loader.name, spec.loader.path)
        return spec


def install():
    sys.meta_path.insert(0, _Finder())


def dump():
    out = os.environ.get("MAP_OUT", "/out")
    with open(os.path.join(out, f"cmp.{os.getpid()}.{time.time_ns()}.json"), "w") as f:
        json.dump({"test": TEST, "hit": sorted(_hit), "differ": {s: sorted(v) for s, v in _differ.items()}}, f)
