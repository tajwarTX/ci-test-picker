"""Container-entry usage (mapper only, never in the submission).

While mapping, sqlglot modules are loaded through an import hook that rewrites every
class-body or module-level assignment whose value is a dict/set display (or a | - & ^
combination of displays and other containers) into a tracked container:

  * TrackedDict / TrackedSet record, per test process, which keys/elements were looked
    up (d[k], d.get(k), k in d, ...) and whether the container was used wholesale
    (iterated, copied, compared, measured, merged, pickled, mutated ...).
  * Containers built at import from other tracked containers ({**Base.X, ...},
    Base.X | {...}) record where they came from, so a change to a base entry can be
    followed into every copy.
  * Each explicit entry's source lines and canonical key are recorded.

Written at exit to $MAP_OUT/ceu.<pid>.<ns>.json.
"""

import ast
import builtins
import enum
import importlib.abc
import importlib.machinery
import json
import os
import sys
import time

SRC = os.environ.get("MAP_SRC", "/src")
TEST = os.environ.get("MAP_TEST", "")

_acc: dict = {}      # cid -> set(canonical keys looked up)
_whole: set = set()  # cids used wholesale
_whole_at: dict = {}  # cid -> {(file, line)} of the Python frames that used it wholesale
_infn: set = set()  # cids of displays inside functions (ALL_CAPS attribute assignments)
_src: dict = {}      # cid -> set(source cids)
_entries: dict = {}  # cid -> [[start, end, canon or None], ...]
_kind: dict = {}     # cid -> "dict" | "set"
_where: dict = {}    # cid -> [file, start, end] of the display/expression


def canon(k):
    """A name for a key that is stable across processes and follows Python equality."""
    try:
        if k is None:
            return "none"
        if isinstance(k, str):  # str-based enums compare as their string value
            return "str:" + str.__str__(k)
        if isinstance(k, (bool, int, float)) and not isinstance(k, enum.Enum) or isinstance(k, (enum.IntEnum, enum.IntFlag)):
            return "num:" + repr(float(k) if float(k) == k else k)
        if isinstance(k, enum.Enum):
            t = type(k)
            return f"enum:{t.__module__}.{t.__qualname__}.{k.name}"
        if isinstance(k, type):
            return f"cls:{k.__module__}.{k.__qualname__}"
        if isinstance(k, tuple):
            parts = [canon(x) for x in k]
            return None if None in parts else "tuple:(" + ",".join(parts) + ")"
    except Exception:
        return None
    return None


def _look(cid, key):
    c = canon(key)
    if c is None:
        _whole.add(cid)
        _whole_at.setdefault(cid, set()).add(("?unnamed-key", 0))
    else:
        _acc.setdefault(cid, set()).add(c)


def _mark_whole(cid):
    _whole.add(cid)
    f = sys._getframe(2)
    _whole_at.setdefault(cid, set()).add((f.f_code.co_filename, f.f_lineno))


def _wholesale(name):
    def method(self, *a, **k):
        _mark_whole(self._ceu_cid)
        return getattr(self._ceu_base, name)(self, *a, **k)

    method.__name__ = name
    return method


class TrackedDict(dict):
    _ceu_base = dict
    __slots__ = ("_ceu_cid",)

    def __getitem__(self, k):
        _look(self._ceu_cid, k)
        return dict.__getitem__(self, k)

    def get(self, k, *default):
        _look(self._ceu_cid, k)
        return dict.get(self, k, *default)

    def __contains__(self, k):
        _look(self._ceu_cid, k)
        return dict.__contains__(self, k)

    def setdefault(self, k, *default):
        _look(self._ceu_cid, k)
        return dict.setdefault(self, k, *default)

    def pop(self, k, *default):
        _look(self._ceu_cid, k)
        return dict.pop(self, k, *default)

    def __reduce_ex__(self, protocol):
        _mark_whole(self._ceu_cid)
        return (_rebuild_dict, (self._ceu_cid, dict(dict.items(self))))


for _n in ("__iter__", "keys", "values", "items", "copy", "__len__", "__eq__", "__ne__",
           "__repr__", "__str__", "__reversed__", "__or__", "__ror__", "__ior__", "update",
           "__setitem__", "__delitem__", "popitem", "clear", "__format__", "__sizeof__"):
    setattr(TrackedDict, _n, _wholesale(_n))
TrackedDict.__hash__ = None


class TrackedSet(set):
    _ceu_base = set
    __slots__ = ("_ceu_cid",)

    def __contains__(self, k):
        _look(self._ceu_cid, k)
        return set.__contains__(self, k)

    def __reduce_ex__(self, protocol):
        _mark_whole(self._ceu_cid)
        return (_rebuild_set, (self._ceu_cid, list(set.__iter__(self))))


for _n in ("__iter__", "__len__", "__or__", "__ror__", "__and__", "__rand__", "__sub__",
           "__rsub__", "__xor__", "__rxor__", "__ior__", "__iand__", "__isub__", "__ixor__",
           "union", "intersection", "difference", "symmetric_difference", "issubset",
           "issuperset", "isdisjoint", "copy", "__eq__", "__ne__", "__le__", "__lt__",
           "__ge__", "__gt__", "__repr__", "__str__", "add", "discard", "remove", "pop",
           "update", "intersection_update", "difference_update",
           "symmetric_difference_update", "clear", "__format__", "__sizeof__"):
    setattr(TrackedSet, _n, _wholesale(_n))
TrackedSet.__hash__ = None


def _rebuild_dict(cid, data):
    d = TrackedDict(data)
    d._ceu_cid = cid
    return d


def _rebuild_set(cid, data):
    s = TrackedSet(data)
    s._ceu_cid = cid
    return s


def _cid_of(obj):
    return getattr(obj, "_ceu_cid", None) if isinstance(obj, (TrackedDict, TrackedSet)) else None


def __ceu_dict__(cid, where, items):
    """items: ("u", mapping) for **mapping, or ("k", start, end, key, value)."""
    d = TrackedDict()
    d._ceu_cid = cid
    entries = []
    for it in items:
        if it[0] == "u":
            src = it[1]
            s = _cid_of(src)
            if s and isinstance(src, TrackedDict):
                _src.setdefault(cid, set()).add(s)
                dict.update(d, dict.items(src))
            else:
                dict.update(d, src)
        else:
            _, start, end, k, v = it
            dict.__setitem__(d, k, v)
            entries.append([start, end, canon(k)])
    _entries.setdefault(cid, entries)
    _kind[cid] = "dict"
    _where[cid] = where
    return d


def __ceu_set__(cid, where, items):
    s = TrackedSet()
    s._ceu_cid = cid
    entries = []
    for it in items:
        if it[0] == "u":
            src = it[1]
            sc = _cid_of(src)
            if sc:
                _src.setdefault(cid, set()).add(sc)
                set.update(s, dict.keys(src) if isinstance(src, TrackedDict) else set.__iter__(src))
            else:
                set.update(s, src)
        else:
            _, start, end, e = it
            set.add(s, e)
            entries.append([start, end, canon(e)])
    _entries.setdefault(cid, entries)
    _kind[cid] = "set"
    _where[cid] = where
    return s


_OPS = {"|": lambda a, b: a | b, "-": lambda a, b: a - b, "&": lambda a, b: a & b, "^": lambda a, b: a ^ b}


def __ceu_binop__(cid, where, op, left, right):
    lc, rc = _cid_of(left), _cid_of(right)
    plain = lambda x: (dict(dict.items(x)) if isinstance(x, TrackedDict)
                       else set(set.__iter__(x)) if isinstance(x, TrackedSet) else x)
    result = _OPS[op](plain(left), plain(right))
    if isinstance(result, dict):
        out = TrackedDict(result)
        _kind[cid] = "dict"
    elif isinstance(result, set):
        out = TrackedSet(result)
        _kind[cid] = "set"
    else:
        return result
    out._ceu_cid = cid
    for c in (lc, rc):
        if c:
            _src.setdefault(cid, set()).add(c)
    _entries.setdefault(cid, [])
    _where[cid] = where
    return out


builtins.__ceu_dict__ = __ceu_dict__
builtins.__ceu_set__ = __ceu_set__
builtins.__ceu_binop__ = __ceu_binop__

_BINOPS = {ast.BitOr: "|", ast.Sub: "-", ast.BitAnd: "&", ast.BitXor: "^"}


import re as _re

_CAPS = _re.compile(r"[A-Z][A-Z0-9_]*\Z").match
_INFN_SITES: set = set()


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def _cid(self, node):
        return f"{self.rel}:{node.lineno}:{node.col_offset}"

    def _where(self, node):
        return ast.Tuple([ast.Constant(self.rel), ast.Constant(node.lineno), ast.Constant(node.end_lineno)], ast.Load())

    def wrap(self, node):
        """Rewrite a container expression; return None if it is not one we track."""
        if isinstance(node, ast.Dict):
            items = []
            for k, v in zip(node.keys, node.values):
                if k is None:
                    items.append(ast.Tuple([ast.Constant("u"), self.wrap(v) or v], ast.Load()))
                else:
                    items.append(ast.Tuple([ast.Constant("k"), ast.Constant(k.lineno), ast.Constant(v.end_lineno), k, v], ast.Load()))
            return ast.Call(ast.Name("__ceu_dict__", ast.Load()),
                            [ast.Constant(self._cid(node)), self._where(node), ast.Tuple(items, ast.Load())], [])
        if isinstance(node, ast.Set):
            items = []
            for e in node.elts:
                if isinstance(e, ast.Starred):
                    items.append(ast.Tuple([ast.Constant("u"), self.wrap(e.value) or e.value], ast.Load()))
                else:
                    items.append(ast.Tuple([ast.Constant("e"), ast.Constant(e.lineno), ast.Constant(e.end_lineno), e], ast.Load()))
            return ast.Call(ast.Name("__ceu_set__", ast.Load()),
                            [ast.Constant(self._cid(node)), self._where(node), ast.Tuple(items, ast.Load())], [])
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            left, right = self.wrap(node.left), self.wrap(node.right)
            if left is None and right is None and not any(
                isinstance(x, (ast.Name, ast.Attribute)) for x in (node.left, node.right)
            ):
                return None
            return ast.Call(ast.Name("__ceu_binop__", ast.Load()),
                            [ast.Constant(self._cid(node)), self._where(node), ast.Constant(_BINOPS[type(node.op)]),
                             left or node.left, right or node.right], [])
        return None

    def _assigns(self, body):
        for stmt in body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                if all(isinstance(t, ast.Name) for t in targets):
                    new = self.wrap(stmt.value)
                    if new is not None:
                        stmt.value = new
            elif isinstance(stmt, ast.ClassDef):
                self._assigns(stmt.body)

    def visit_Module(self, node):
        self._assigns(node.body)
        for fn in ast.walk(node):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for stmt in ast.walk(fn):
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and \
                        isinstance(stmt.targets[0], ast.Attribute) and _CAPS(stmt.targets[0].attr):
                    new = self.wrap(stmt.value)
                    if new is not None:
                        _INFN_SITES.add(self._cid(stmt.value))
                        stmt.value = new
        return node


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        source = self.get_data(path)
        tree = ast.parse(source, path)
        rel = os.path.relpath(path, SRC)
        tree = _Rewriter(rel).visit(tree)
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
    data = {
        "test": TEST,
        "acc": {c: sorted(v) for c, v in _acc.items()},
        "whole": sorted(_whole),
        "whole_at": {c: sorted(f"{os.path.relpath(fn, SRC)}:{ln}" for fn, ln in v) for c, v in _whole_at.items()},
        "infn": sorted(_INFN_SITES & set(_kind)),
        "src": {c: sorted(v) for c, v in _src.items()},
        "entries": _entries,
        "kind": _kind,
        "where": _where,
    }
    with open(os.path.join(out, f"ceu.{os.getpid()}.{time.time_ns()}.json"), "w") as f:
        json.dump(data, f)
