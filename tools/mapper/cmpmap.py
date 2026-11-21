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

Conservative cases (every partner counts as differing, nothing is probed):
  * the original comparison raised;
  * the original result is not a bool (e.g. an expression builder).

Probes must not change what the test does afterwards:
  * Expression.__hash__ caches hashes in `node._hash`; stores made while probing are
    logged and undone when the probe ends (the rewrite turns `X._hash = V` into a call).
  * if a probe imports a module (Dialect.get can), that cannot be undone, so the
    process is marked "tainted" and build_map refuses to narrow with it.

Written at exit to $MAP_OUT/cmp.<pid>.<ns>.json as
    {"test": id, "hit": [site...], "differ": {site: [partner ops]}, "tainted": bool}
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
_tainted = False
_local = threading.local()  # per thread: "probing" flag and the undo log of _hash stores


def _all_differ(site, op):
    _differ.setdefault(site, set()).update(PARTNERS[op])


def __cmp__(site, op, left, right):
    global _tainted
    busy = getattr(_local, "busy", False)
    try:
        result = OPS[op](left, right)  # the original comparison, unchanged
    except BaseException:
        if not busy:
            _hit.add(site)
            _all_differ(site, op)
        raise
    if busy:
        return result
    _hit.add(site)
    done = _differ.get(site)
    partners = PARTNERS[op]
    if done is not None and len(done) == len(partners):
        return result
    if type(result) is not bool:
        _all_differ(site, op)
        return result
    _local.busy = True  # comparisons run by __eq__ & co. while probing are not recorded
    _local.undo = []
    n_modules = len(sys.modules)
    try:
        for p in partners:
            if done is not None and p in done:
                continue
            try:
                other = OPS[p](left, right)
                same = type(other) is bool and result is other
            except Exception:
                same = False
            if not same:
                _differ.setdefault(site, set()).add(p)
                done = _differ[site]
    finally:
        for obj, old in reversed(_local.undo):
            obj._hash = old
        _local.undo = None
        _local.busy = False
        if len(sys.modules) != n_modules:
            _tainted = True
    return result


def __cmp_sethash__(obj, value):
    undo = getattr(_local, "undo", None)
    if undo is not None:
        undo.append((obj, getattr(obj, "_hash", None)))
    obj._hash = value


def __cmp_effect__():
    """Called at the start of functions that change global state (Dialect._try_load)."""
    global _tainted
    if getattr(_local, "busy", False):
        _tainted = True


builtins.__cmp__ = __cmp__
builtins.__cmp_sethash__ = __cmp_sethash__
builtins.__cmp_effect__ = __cmp_effect__

EFFECT_FUNCS = {("sqlglot/dialects/dialect.py", "_try_load")}


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        if (self.rel, node.name) in EFFECT_FUNCS:
            call = ast.Expr(ast.Call(ast.Name("__cmp_effect__", ast.Load()), [], []))
            ast.copy_location(call, node.body[0])
            first = node.body[0]
            doc = isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                and isinstance(first.value.value, str)
            node.body.insert(1 if doc else 0, call)
        return node

    def visit_Assign(self, node):
        self.generic_visit(node)
        if len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Attribute) and t.attr == "_hash" and isinstance(t.value, ast.Name):
                call = ast.Call(ast.Name("__cmp_sethash__", ast.Load()),
                                [ast.Name(t.value.id, ast.Load()), node.value], [])
                return ast.copy_location(ast.Expr(ast.copy_location(call, node)), node)
        return node

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
        json.dump({"test": TEST, "hit": sorted(_hit), "differ": {s: sorted(v) for s, v in _differ.items()},
                   "tainted": _tainted}, f)
