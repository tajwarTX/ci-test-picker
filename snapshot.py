"""Import-time state snapshot of sqlglot (stdlib only).

After importing sqlglot, every dialect and the executor/planner/optimizer, every module
global of every loaded sqlglot module and every entry of every class defined in those
modules is hashed with a hash that is the same in every process (no ids, no hash()).

  python snapshot.py build OUT.json.gz   pristine hashes
  python snapshot.py check OUT.json.gz   re-run (other PYTHONHASHSEED), mark unstable keys
  python snapshot.py diff OUT.json.gz    one JSON line: changed / added / removed keys of the
                                          sqlglot on PYTHONPATH against OUT, with kinds

Keys: "module:attr" (module globals), "module:Qual.Name:attr" (class __dict__ entries),
"module:Qual.Name:__bases__" / "__mro__" (by qualified names).
Values that cannot be hashed stably are "opaque:<type>"; they are listed in the build file
("opaque") because a change to one of them cannot be seen.
"""

import enum
import functools
import gzip
import hashlib
import json
import os
import re
import sys
import time
import types

_SKIP = {"__module__", "__qualname__", "__doc__", "__dict__", "__weakref__", "__firstlineno__"}
_FUNC_LIKE = (types.FunctionType, types.MethodType, staticmethod, classmethod, property,
              functools.partial, functools.cached_property)


_FAST = {
    type(None): lambda o: b"N",
    bool: lambda o: b"b1" if o else b"b0",
    int: lambda o: b"i:" + str(o).encode(),
    float: lambda o: b"f:" + repr(o).encode(),
    complex: lambda o: b"c:" + repr(o).encode(),
    str: None,  # cached in the hasher
    bytes: lambda o: b"y:" + o,
}
_FAST[str] = lambda o: b""  # placeholder; enc handles str itself


def _qual(obj):
    return f"{getattr(obj, '__module__', '?')}.{getattr(obj, '__qualname__', getattr(obj, '__name__', '?'))}"


class _Hasher:
    def __init__(self):
        self.memo = {}      # id -> digest (finished objects)
        self.active = set()  # ids on the current path
        self.keep = []      # keep memoized objects alive (ids stay unique)
        self.opaque = False
        self.strs = {}

    def h(self, obj, depth=0):
        return hashlib.sha1(self.enc(obj, depth)).digest()

    def enc(self, obj, depth):
        t = type(obj)
        f = _FAST.get(t)
        if f is not None:
            if t is str:
                r = self.strs.get(obj)
                if r is None:
                    r = self.strs[obj] = b"s:" + obj.encode("utf-8", "surrogatepass")
                return r
            return f(obj)
        if isinstance(obj, enum.Enum):
            return ("enum:" + _qual(t) + "." + obj.name).encode()
        if isinstance(obj, type):
            r = self.strs.get(obj)
            if r is None:
                r = self.strs[obj] = ("class:" + _qual(obj)).encode()
            return r
        if t is types.ModuleType:
            return ("module:" + obj.__name__).encode()
        if t is types.BuiltinFunctionType or t is types.BuiltinMethodType:
            return ("builtin:" + str(getattr(obj, "__module__", "?")) + "." + obj.__qualname__).encode()
        if t is re.Pattern:
            return b"re:" + self.enc(obj.pattern, depth) + (":" + str(obj.flags)).encode()
        if depth > 50:
            return b"deep"
        i = id(obj)
        m = self.memo.get(i)
        if m is not None:
            return m
        if i in self.active:
            return ("cycle:" + _qual(t)).encode()
        self.active.add(i)
        try:
            out = self._compound(obj, t, depth + 1)
        finally:
            self.active.discard(i)
        d = b"@" + hashlib.sha1(out).digest()
        self.memo[i] = d
        self.keep.append(obj)
        return d

    def _compound(self, obj, t, depth):
        e = self.enc
        if t in (list, tuple):
            return (b"L[" if t is list else b"T[") + b",".join(e(x, depth) for x in obj) + b"]"
        if isinstance(obj, (set, frozenset)):
            return b"S{" + b",".join(sorted(e(x, depth) for x in obj)) + b"}"
        if isinstance(obj, dict) and t in (dict,) or type(obj).__name__ in ("OrderedDict", "defaultdict") and isinstance(obj, dict):
            items = sorted((e(k, depth), e(v, depth)) for k, v in obj.items())
            extra = b""
            if type(obj).__name__ == "defaultdict":
                extra = b"df:" + e(obj.default_factory, depth)
            return f"D{t.__name__}{{".encode() + extra + b",".join(k + b"=" + v for k, v in items) + b"}"
        if isinstance(obj, types.CodeType):
            return self._code(obj, depth)
        if isinstance(obj, types.FunctionType):
            cells = []
            for c in obj.__closure__ or ():
                try:
                    cells.append(e(c.cell_contents, depth))
                except ValueError:
                    cells.append(b"empty")
            return (b"F" + obj.__qualname__.encode() + self._code(obj.__code__, depth)
                    + b"|d" + e(obj.__defaults__, depth) + b"|k" + e(obj.__kwdefaults__, depth)
                    + b"|c" + b",".join(cells) + b"|D" + e(dict(obj.__dict__), depth))
        if isinstance(obj, types.MethodType):
            return b"M" + e(obj.__func__, depth) + e(obj.__self__, depth)
        if isinstance(obj, (staticmethod, classmethod)):
            return f"{t.__name__}:".encode() + e(obj.__func__, depth)
        if isinstance(obj, property):
            return b"P" + e(obj.fget, depth) + e(obj.fset, depth) + e(obj.fdel, depth)
        if isinstance(obj, functools.partial):
            return b"partial" + e(obj.func, depth) + e(obj.args, depth) + e(dict(obj.keywords), depth)
        if isinstance(obj, functools.cached_property):
            return b"cprop" + e(obj.func, depth)
        if isinstance(obj, dict):  # other dict subclasses
            items = sorted((e(k, depth), e(v, depth)) for k, v in obj.items())
            d = getattr(obj, "__dict__", None)
            return f"D{_qual(t)}{{".encode() + b",".join(k + b"=" + v for k, v in items) + b"}" + (
                e(dict(d), depth) if d else b"")
        if t.__module__ in ("typing", "typing_extensions", "types") and not hasattr(obj, "__dict__"):
            r = repr(obj)
            if " at 0x" not in r:
                return b"typing:" + r.encode()
        if t.__module__ == "logging" or (t.__name__ in ("Logger", "RootLogger", "PlaceHolder")):
            return f"logger:{getattr(obj, 'name', '?')}".encode()
        # generic object: type + instance attributes
        parts = [f"O{_qual(t)}".encode()]
        d = getattr(obj, "__dict__", None)
        had = False
        if isinstance(d, dict):
            had = True
            parts.append(e(dict(d), depth))
        slots = []
        for c in t.__mro__:
            s = c.__dict__.get("__slots__", ())
            slots.extend((s,) if isinstance(s, str) else s)
        for s in sorted(set(slots)):
            if s in ("__dict__", "__weakref__"):
                continue
            had = True
            try:
                parts.append(s.encode() + b"=" + e(getattr(obj, s), depth))
            except AttributeError:
                parts.append(s.encode() + b"=unset")
        if not had:
            r = repr(obj)
            if " at 0x" in r or "object at" in r:
                self.opaque = True
                return f"opaque:{_qual(t)}".encode()
            parts.append(b"repr:" + r.encode())
        return b"|".join(parts)

    def _code(self, co, depth):
        e = self.enc
        consts = b",".join(self._code(c, depth) if isinstance(c, types.CodeType) else e(c, depth)
                           for c in co.co_consts)
        return (b"C" + co.co_code + b"|" + consts + b"|" + "|".join(
            [",".join(co.co_names), ",".join(co.co_varnames), ",".join(co.co_freevars),
             ",".join(co.co_cellvars), str(co.co_argcount), str(co.co_posonlyargcount),
             str(co.co_kwonlyargcount), str(co.co_flags), co.co_name]).encode())


def _home(v):
    """Where a function-like value is defined: "module:qualname" (None if not a plain def)."""
    f = v
    if isinstance(f, (staticmethod, classmethod)):
        f = f.__func__
    elif isinstance(f, property):
        f = f.fget
    elif isinstance(f, types.MethodType):
        f = f.__func__
    if isinstance(f, types.FunctionType) and "<" not in f.__qualname__:
        return f"{f.__module__}:{f.__qualname__}"
    return None


def _kind(v):
    if isinstance(v, type):
        return "class"
    if isinstance(v, _FUNC_LIKE):
        return "function"
    return "value"


def _load():
    import sqlglot  # noqa: F401
    from sqlglot.dialects.dialect import Dialect, Dialects

    for d in Dialects:
        try:
            Dialect.get_or_raise(d.value)
        except Exception:
            pass
    import sqlglot.executor  # noqa: F401
    import sqlglot.executor.python  # noqa: F401
    import sqlglot.executor.table  # noqa: F401
    import sqlglot.optimizer  # noqa: F401
    import sqlglot.planner  # noqa: F401


def snapshot():
    """{key: [digest hex, kind]}, sorted opaque keys."""
    _load()
    H = _Hasher()
    out, opaque = {}, []

    def put(key, v):
        H.opaque = False
        out[key] = [H.h(v).hex()[:20], _kind(v), _home(v)]
        if H.opaque:
            opaque.append(key)
            if out[key][1] == "value":
                out[key][1] = "opaque"

    for name in sorted(n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")):
        mod = sys.modules[name]
        if not isinstance(mod, types.ModuleType):
            continue
        g = vars(mod)
        classes = []
        for k in sorted(g):
            if k in ("__builtins__", "__loader__", "__spec__", "__file__", "__cached__", "__doc__",
                     "__name__", "__package__", "__path__"):
                continue
            v = g[k]
            put(f"{name}:{k}", v)
            if isinstance(v, type) and v.__module__ == name and v.__qualname__ == k:
                classes.append(v)
        seen = set()
        while classes:
            c = classes.pop()
            if id(c) in seen:
                continue
            seen.add(id(c))
            q = c.__qualname__
            put(f"{name}:{q}:__bases__", tuple(_qual(b) for b in c.__bases__))
            put(f"{name}:{q}:__mro__", tuple(_qual(b) for b in c.__mro__))
            for k in sorted(c.__dict__):
                if k in _SKIP:
                    continue
                v = c.__dict__[k]
                put(f"{name}:{q}:{k}", v)
                if isinstance(v, type) and v.__module__ == name and v.__qualname__ == f"{q}.{k}":
                    classes.append(v)
    return out, sorted(opaque)


def main():
    mode, path = sys.argv[1], sys.argv[2]
    t0 = time.process_time()
    snap, opaque = snapshot()
    cpu = time.process_time() - t0
    if mode == "build":
        with gzip.open(path, "wt") as f:
            json.dump({"keys": snap, "opaque": opaque, "unstable": [], "cpu": round(cpu, 3)}, f)
        print(f"keys {len(snap)} opaque {len(opaque)} cpu {cpu:.2f}s", file=sys.stderr)
        return 0
    with gzip.open(path, "rt") as f:
        doc = json.load(f)
    base = doc["keys"]
    if mode == "check":
        bad = sorted(k for k in set(base) | set(snap) if base.get(k, [None])[0] != snap.get(k, [None])[0])
        doc["unstable"] = sorted(set(doc["unstable"]) | set(bad))
        with gzip.open(path, "wt") as f:
            json.dump(doc, f)
        print(f"unstable {len(doc['unstable'])} cpu {cpu:.2f}s", file=sys.stderr)
        return 0
    if mode == "diff":
        unstable = set(doc["unstable"])
        changed, added, removed, touched, kinds, homes = [], [], [], [], {}, {}
        for k in sorted(set(base) | set(snap)):
            a, b = base.get(k), snap.get(k)
            if a is not None and b is not None and a[0] == b[0]:
                continue
            if k in unstable:
                touched.append(k)
                continue
            if a is None:
                added.append(k)
            elif b is None:
                removed.append(k)
            else:
                changed.append(k)
            kinds[k] = (b or a)[1]
            homes[k] = [(x[2] if x is not None and len(x) > 2 else None) for x in (a, b)]
        print(json.dumps({"changed": changed, "added": added, "removed": removed,
                          "unstable_touched": touched, "kinds": kinds, "homes": homes, "cpu": round(cpu, 3)}))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
