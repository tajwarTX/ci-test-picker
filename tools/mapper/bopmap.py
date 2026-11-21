"""and/or profile (mapper only, never in the submission).

sqlglot is loaded through an import hook that rewrites every two-operand `a and b` /
`a or b` into

    __bl__(site, is_and, a)  and/or  __br__(site, ctx, b)

which return their operand unchanged (so short-circuiting and values are exactly as
before) and record, per evaluation, whether swapping `and` <-> `or` could change the
result ("infected"):

  * the original short-circuited (and: a falsy; or: a truthy): the swapped operator
    would evaluate b, which the original never did -> infected;
  * otherwise the original result is b and the swapped one is a: same only if `a is b`,
    or, where only truthiness is used (if/while/assert/ternary test, `not`, comprehension
    filter, or inside such a BoolOp), bool(b) equals bool(a);
  * b raised (the pair never completed) -> infected.
  * the swapped operator would SKIP evaluating b, so b must have no side effects: b may
    not assign (walrus), yield or await (static), and while b is evaluated no Python
    function and no builtin other than read-only ones may run (sys.setprofile, active
    only during b) -> otherwise infected.

Nothing is evaluated that the original program does not evaluate, except an extra
bool() of a value the program itself tests for truth.

Written at exit to $MAP_OUT/bop.<pid>.<ns>.json as {"test", "hit", "infected"}.
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import sys
import threading
import time

SRC = os.environ.get("MAP_SRC", "/src")
TEST = os.environ.get("MAP_TEST", "")

_hit: set = set()
_infected: set = set()
_local = threading.local()


# read-only builtins: calling them while the skipped operand is evaluated is harmless
_PURE_C = {isinstance, issubclass, len, type, id, callable, hasattr, getattr, bool}
_PURE_METHODS = {
    dict: {"get", "keys", "values", "items", "__contains__", "__getitem__", "copy"},
    list: {"index", "count", "__getitem__", "__contains__", "copy"},
    set: {"__contains__", "issubset", "issuperset", "isdisjoint", "union", "intersection", "difference", "copy"},
}
_IMMUTABLE = (str, bytes, tuple, frozenset, int, float, bool, type(None))


def _c_pure(fn):
    if fn in _PURE_C:
        return True
    owner = getattr(fn, "__self__", None)
    if isinstance(owner, _IMMUTABLE):
        return True
    names = _PURE_METHODS.get(type(owner))
    return names is not None and getattr(fn, "__name__", "") in names


def _prof(frame, event, arg):
    """Active only while some skipped operand is being evaluated: any Python call, or a
    builtin call that is not read-only, makes every open window impure."""
    if not _local.__dict__.get("watch"):
        return  # nothing is being watched (the common case): cheap exit
    if frame.f_code in _HELPERS:
        return
    if event == "call" or (event == "c_call" and not _c_pure(arg)):
        for w in _local.st:
            w[2] = True
        _local.watch = 0  # every open window is impure now


def __bl__(site, is_and, a, pure_static):
    _hit.add(site)
    t = bool(a)
    if t is not is_and or not pure_static:
        # short-circuit (the swapped operator would evaluate b), or b assigns/yields:
        # the swap can change this run
        _infected.add(site)
        return a
    st = getattr(_local, "st", None)
    if st is None:
        st = _local.st = []
    key = (id(sys._getframe(1)), site)
    for w in st:
        if w[0] == key:  # an earlier pair at this site in this frame never completed
            _infected.add(site)
    st.append([key, (t, a), False])
    _local.watch = 1  # watch until b is evaluated, or until its first call
    return a


def __br__(site, ctx, b):
    st = getattr(_local, "st", None) or []
    key = (id(sys._getframe(1)), site)
    got = None
    while st:
        w = st.pop()
        if w[0] == key:
            got = w
            break
        _infected.add(w[0][1])  # a pair that never completed (its operand raised)
    _local.watch = 1 if any(not w[2] for w in st) else 0
    if got is None:
        _infected.add(site)
        return b
    (t, a), impure = got[1], got[2]
    if impure:
        # evaluating b ran code that may have changed state; the swapped operator
        # would skip it
        _infected.add(site)
        return b
    # the swapped operator would return a instead of b
    if a is b:
        return b
    if ctx:
        try:
            if bool(b) == t:
                return b
        except Exception:
            pass
    _infected.add(site)
    return b


_HELPERS = {__bl__.__code__, __br__.__code__, _prof.__code__}
builtins.__bl__ = __bl__
builtins.__br__ = __br__


def _bool_context(tree):
    """ids of BoolOp nodes whose value is only tested for truth."""
    ctx = set()

    def mark(e):
        if isinstance(e, ast.BoolOp):
            ctx.add(id(e))
            for v in e.values:
                mark(v)

    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            mark(node.test)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            mark(node.operand)
        elif isinstance(node, ast.comprehension):
            for i in node.ifs:
                mark(i)
    return ctx


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel, ctx):
        self.rel, self.ctx = rel, ctx

    def visit_BoolOp(self, node):
        in_ctx = id(node) in self.ctx
        self.generic_visit(node)
        if len(node.values) != 2:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        a, b = node.values
        is_and = isinstance(node.op, ast.And)
        pure_static = not any(isinstance(n, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await))
                              for n in ast.walk(b))
        node.values = [
            ast.copy_location(ast.Call(ast.Name("__bl__", ast.Load()),
                                       [ast.Constant(site), ast.Constant(is_and), a, ast.Constant(pure_static)], []), a),
            ast.copy_location(ast.Call(ast.Name("__br__", ast.Load()),
                                       [ast.Constant(site), ast.Constant(in_ctx), b], []), b),
        ]
        return node


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        tree = ast.parse(self.get_data(path), path)
        ctx = _bool_context(tree)
        tree = _Rewriter(os.path.relpath(path, SRC), ctx).visit(tree)
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
    # installed once (switching a profiler on and off re-instruments code: far too slow);
    # threads started later get it too
    sys.setprofile(_prof)
    threading.setprofile(_prof)


def dump():
    inf = set(_infected)
    inf |= {w[0][1] for w in getattr(_local, "st", None) or []}  # pairs that never completed
    out = os.environ.get("MAP_OUT", "/out")
    with open(os.path.join(out, f"bop.{os.getpid()}.{time.time_ns()}.json"), "w") as f:
        json.dump({"test": TEST, "hit": sorted(_hit), "infected": sorted(inf), "version": 2}, f)
