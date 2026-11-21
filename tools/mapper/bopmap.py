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


def _pending():
    p = getattr(_local, "p", None)
    if p is None:
        p = _local.p = {}
    return p


def __bl__(site, is_and, a):
    _hit.add(site)
    t = bool(a)
    if t is not is_and:  # short-circuit: the swapped operator would evaluate b
        _infected.add(site)
        return a
    key = (id(sys._getframe(1)), site)
    p = _pending()
    if key in p:  # an earlier pair at this site in this frame never completed (b raised)
        _infected.add(site)
    p[key] = (t, a)
    return a


def __br__(site, ctx, b):
    key = (id(sys._getframe(1)), site)
    got = _pending().pop(key, None)
    if got is None:
        _infected.add(site)
        return b
    t, a = got
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
        node.values = [
            ast.copy_location(ast.Call(ast.Name("__bl__", ast.Load()),
                                       [ast.Constant(site), ast.Constant(is_and), a], []), a),
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


def dump():
    inf = set(_infected)
    p = getattr(_local, "p", None) or {}
    inf |= {site for (_, site) in p}  # pairs that never completed
    out = os.environ.get("MAP_OUT", "/out")
    with open(os.path.join(out, f"bop.{os.getpid()}.{time.time_ns()}.json"), "w") as f:
        json.dump({"test": TEST, "hit": sorted(_hit), "infected": sorted(inf)}, f)
