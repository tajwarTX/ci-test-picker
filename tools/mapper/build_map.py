"""Turn raw coverage runs into data/map.json.gz (the file the selector reads).

Run inside the br-mapper image:
  python /plugin/build_map.py /data/tests.json /out/map.json.gz RUN_DIR [RUN_DIR ...]
Each RUN_DIR holds .coverage* files and opened*.json from covmap_plugin.

Map layout:
  sets:   list of sorted test-number lists (deduplicated)
  files:  {relpath: {"all": set_id, "funcs": [[def_start, body_start, end, set_id], ...]}}
          "all"   = tests whose process ran ANY line of the file (incl. imports)
          "funcs" = for each function: tests that ran any line of its body
  opened: {relpath: set_id}  non-Python files (or dirs) a test opened / listed
"""

import ast
import glob
import gzip
import json
import os
import sys


SRC = "/src"
tests = json.load(open(sys.argv[1]))["tests"]
out_path = sys.argv[2]
run_dirs = sys.argv[3:]
number = {t["id"]: t["number"] for t in tests}
unknown_ctx = set()


def ctx_to_test(ctx):
    if not ctx:
        return None
    if ctx.startswith("C|"):
        ctx = ctx[2:]
    n = number.get(ctx)
    if n is None:
        unknown_ctx.add(ctx)
    return n


# line_tests[file][line] = set(test numbers)
line_tests: dict[str, dict[int, set]] = {}
opened: dict[str, set] = {}

import sqlite3
from coverage.parser import PythonParser
from coverage.numbits import numbits_to_nums

for run in run_dirs:
    files = glob.glob(os.path.join(run, ".coverage*"))
    print(f"{run}: {len(files)} coverage files", file=sys.stderr)
    for path in files:
        # Read the SQLite file directly: much faster than CoverageData for thousands of files.
        con = sqlite3.connect(path)
        fname = dict(con.execute("select id, path from file"))
        cname = dict(con.execute("select id, context from context"))
        for file_id, ctx_id, bits in con.execute("select file_id, context_id, numbits from line_bits"):
            n = ctx_to_test(cname[ctx_id])
            rel = os.path.relpath(fname[file_id], SRC)
            per = line_tests.setdefault(rel, {})
            for line in numbits_to_nums(bits):
                s = per.setdefault(line, set())
                if n is not None:
                    s.add(n)
        con.close()
    for path in glob.glob(os.path.join(run, "opened*.json")):
        for ctx, paths in json.load(open(path)).items():
            n = ctx_to_test(ctx)
            for p in paths:
                s = opened.setdefault(p, set())
                if n is not None:
                    s.add(n)

# usage[module:Qual.Name] = tests that ran a method of that class (or a subclass)
usage: dict[str, set] = {}
for run in run_dirs:
    for path in glob.glob(os.path.join(run, "usage*.json")):
        for ctx, keys in json.load(open(path)).items():
            n = ctx_to_test(ctx)
            if n is None:
                continue
            for k in keys:
                usage.setdefault(k, set()).add(n)

# Narrowing by class usage is only safe if EVERY test's usage was recorded.
if usage:
    covered = set().union(*usage.values())
    missing = [t["id"] for t in tests if t["number"] not in covered]
    if missing:
        sys.exit(f"REFUSING TO BUILD: {len(missing)} tests have no class-usage data, e.g. {missing[:3]}")

set_ids: dict[tuple, int] = {}


def module_name(rel):
    mod = rel[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def classes(tree, mod):
    """[header_start, body_start, end, usage_set_id or -1] for every class (nested too)."""
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                q = f"{prefix}{child.name}"
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                key = f"{mod}:{q}"
                out.append([start, child.body[0].lineno, child.end_lineno,
                            sid(usage[key]) if usage.get(key) else -1])
                walk(child, q + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, f"{prefix}{child.name}.<locals>.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return sorted(out)


def sid(s):
    key = tuple(sorted(s))
    if key not in set_ids:
        set_ids[key] = len(set_ids)
    return set_ids[key]


def functions(tree):
    """(def_start, body_start, end) for every def, innermost found later by smallest span."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out.append((start, node.body[0].lineno, node.end_lineno))
    return out


UPPER = __import__("re").compile(r"^[A-Z][A-Z0-9_]*$")
# name_refs[NAME] = tests that ran a statement inside a function that mentions NAME
# (attribute, bare name, keyword or string constant); name_bad = NAME is also mentioned
# outside any function (import-time use), so a change to it cannot be narrowed.
name_refs: dict[str, set] = {}
name_bad: set = set()
# class_assigns[rel] = [(start, end, [names])] simple UPPER assignments in class bodies
class_assigns: dict[str, list] = {}


def header_lines(stmt):
    body = getattr(stmt, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        return range(stmt.lineno, max(stmt.lineno, body[0].lineno - 1) + 1)
    return range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1)


def scan_names(rel, tree, per, body_lines):
    assigns = []

    def ref(name, stmt):
        if not UPPER.match(name):
            return
        if stmt is None or stmt.lineno not in body_lines:
            name_bad.add(name)
            return
        acc = name_refs.setdefault(name, set())
        for line in header_lines(stmt):
            acc |= per.get(line, set())

    def visit(node, stmt, owner):
        if isinstance(node, ast.stmt):
            stmt = node
            if isinstance(owner, (ast.ClassDef, ast.Module)) and isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if names and len(names) == len(targets) and all(UPPER.match(n) for n in names):
                    assigns.append((node.lineno, node.end_lineno, names,
                                    "class" if isinstance(owner, ast.ClassDef) else "module"))
        definition = (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                      and isinstance(owner, (ast.ClassDef, ast.Module)))
        if not definition:
            if isinstance(node, ast.Name):
                ref(node.id, stmt)
            elif isinstance(node, ast.Attribute):
                ref(node.attr, stmt)
            elif isinstance(node, ast.keyword) and node.arg:
                ref(node.arg, stmt)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 80:
                ref(node.value, stmt)
        for child in ast.iter_child_nodes(node):
            child_owner = node if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)) else owner
            # statements directly inside a class/def/module body have that node as owner;
            # anything deeper keeps the nearest enclosing one
            visit(child, stmt if not isinstance(child, ast.stmt) else None, child_owner)

    visit(tree, None, None)
    class_assigns[rel] = assigns


files_out = {}
for rel, per in sorted(line_tests.items()):
    src = os.path.join(SRC, rel)
    all_tests = set().union(*per.values()) if per else set()
    try:
        tree = ast.parse(open(src).read())
    except Exception as e:
        print("cannot parse", rel, e, file=sys.stderr)
        files_out[rel] = {"all": sid(all_tests), "funcs": []}
        continue
    funcs = []
    body_lines = set()
    for start, body, end in functions(tree):
        s = set()
        for line in range(body, end + 1):
            s |= per.get(line, set())
            body_lines.add(line)
        funcs.append([start, body, end, sid(s)])

    # Statement level, inside function bodies only: which tests ran each executable
    # statement. A multi-line statement is credited with every line it spans.
    text = open(src).read()
    parser = PythonParser(text=text, filename=src)
    parser.parse_source()
    n_lines = text.count("\n") + 1
    first = {}
    for line in range(1, n_lines + 1):
        f = parser.first_line(line)
        if f != line:
            first[line] = f
    span: dict[int, set] = {}
    for line in range(1, n_lines + 1):
        f = first.get(line, line)
        if f in parser.statements and f in body_lines:
            span.setdefault(f, set()).update(per.get(line, set()))
    stmts = [[f, sid(s)] for f, s in sorted(span.items())]
    multi = [[l, f] for l, f in sorted(first.items()) if f in span]
    files_out[rel] = {"all": sid(all_tests), "funcs": sorted(funcs), "stmts": stmts, "multi": multi,
                      "classes": classes(tree, module_name(rel)) if usage else []}
    scan_names(rel, tree, per, body_lines)

# Class-body settings that can be narrowed: every assigned name is UPPER_CASE, is
# mentioned somewhere inside a function, and never outside one.
for rel, assigns in class_assigns.items():
    out, mout = [], []
    for start, end, names, where in assigns:
        if all(n in name_refs and n not in name_bad for n in names):
            (out if where == "class" else mout).append(
                [start, end, sid(set().union(*(name_refs[n] for n in names)))])
    files_out[rel]["settings"] = out
    files_out[rel]["msettings"] = mout

# ---- container-entry usage (ceu*.json) --------------------------------------------
ceu_files = [f for run in run_dirs for f in glob.glob(os.path.join(run, "ceu*.json"))]
containers_out = {}
if ceu_files:
    W: dict = {}
    A: dict = {}
    edges: dict = {}
    kind, where, entries = {}, {}, {}
    seen_tests = set()
    for path in ceu_files:
        d = json.load(open(path))
        n = ctx_to_test(d["test"])
        if n is None:
            continue
        seen_tests.add(n)
        for c in d["whole"]:
            W.setdefault(c, set()).add(n)
        for c, keys in d["acc"].items():
            per = A.setdefault(c, {})
            for k in keys:
                per.setdefault(k, set()).add(n)
        for c, srcs in d["src"].items():
            for s_ in srcs:
                edges.setdefault(s_, set()).add(c)
        kind.update(d["kind"])
        where.update(d["where"])
        for c, e in d["entries"].items():
            entries.setdefault(c, e)
    missing = [t["id"] for t in tests if t["number"] not in seen_tests]
    if missing:
        sys.exit(f"REFUSING TO BUILD: {len(missing)} tests have no container data, e.g. {missing[:3]}")

    def closure(c):
        out, todo = {c}, [c]
        while todo:
            for x in edges.get(todo.pop(), ()):
                if x not in out:
                    out.add(x)
                    todo.append(x)
        return out

    # Sets: Python can copy a set internally (set(S), frozenset(S), x |= S, ...) without
    # calling any tracked method, so a set is only narrowed when its NAME is used
    # nowhere except as `... in NAME` / `... not in NAME` (or inside another tracked
    # container display at class/module level).
    set_names = {}
    for c, k in kind.items():
        if k == "set":
            rel, line = where[c][0], where[c][1]
            set_names.setdefault(rel, {})[line] = None
    _parsed = {}

    def assigned_name(rel, line):
        if rel not in _parsed:
            try:
                _parsed[rel] = ast.parse(open(os.path.join(SRC, rel)).read())
            except Exception:
                _parsed[rel] = None
        tree = _parsed[rel]
        if tree is None:
            return None
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                if node.value.lineno <= line <= (node.value.end_lineno or node.value.lineno):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names = [t.id for t in targets if isinstance(t, ast.Name)]
                    return names[0] if len(names) == 1 else None
        return None
    unsafe_names = set()
    candidate_names = set()
    cid_name = {}
    for c, k in kind.items():
        if k == "set":
            nm = assigned_name(where[c][0], where[c][1])
            cid_name[c] = nm
            if nm:
                candidate_names.add(nm)
    for py in glob.glob(os.path.join(SRC, "sqlglot", "**", "*.py"), recursive=True) + \
              glob.glob(os.path.join(SRC, "tests", "**", "*.py"), recursive=True):
        try:
            tree = ast.parse(open(py).read())
        except Exception:
            continue
        parents = {}
        for node in ast.walk(tree):
            for ch_ in ast.iter_child_nodes(node):
                parents[ch_] = node
        for node in ast.walk(tree):
            nm = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if nm not in candidate_names or isinstance(getattr(node, "ctx", None), ast.Store):
                continue
            par = parents.get(node)
            ok = isinstance(par, ast.Compare) and node in par.comparators and \
                all(isinstance(op, (ast.In, ast.NotIn)) for op in par.ops)
            if not ok:
                # inside a class/module-level container display or | - & ^ of them
                up = node
                while up in parents and isinstance(parents[up], (ast.Set, ast.Dict, ast.BinOp, ast.Starred)):
                    up = parents[up]
                stmt = parents.get(up)
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and up is stmt.value and \
                        isinstance(parents.get(stmt), (ast.Module, ast.ClassDef)) and up is not node:
                    ok = True
            if not ok:
                unsafe_names.add(nm)

    narrowable = 0
    for c, k in kind.items():
        if k == "set" and (cid_name.get(c) is None or cid_name[c] in unsafe_names):
            continue
        cl = closure(c)
        whole = set().union(*(W.get(x, set()) for x in cl))
        if len(whole) == len(tests):
            continue  # used wholesale by every test: never narrowed
        keys = set().union(*(A.get(x, {}).keys() for x in cl))
        acc = {key: sid(set().union(*(A.get(x, {}).get(key, set()) for x in cl))) for key in keys}
        rel, start, end = where[c]
        containers_out[c] = {"file": rel, "start": start, "end": end, "kind": k,
                             "entries": entries.get(c, []), "whole": sid(whole), "acc": acc}
        narrowable += 1
    print(f"containers: {len(kind)} tracked, {narrowable} narrowable, set names unsafe: {len(unsafe_names)}",
          file=sys.stderr)

# ---- comparison-operator profile (cmp*.json) ----------------------------------------
cmp_files_out, cmp_differ_out = [], {}
cmp_paths = [f for run in run_dirs for f in glob.glob(os.path.join(run, "cmp.*.json"))]
if cmp_paths:
    seen_c, tainted_c = set(), set()
    differ: dict = {}
    hit_files = set()
    for path in cmp_paths:
        d = json.load(open(path))
        n = ctx_to_test(d["test"])
        if n is None:
            continue
        seen_c.add(n)
        if d.get("tainted", True):
            tainted_c.add(d["test"])
        for site in d["hit"]:
            hit_files.add(site.rsplit(":", 2)[0])
        for site, ops in d["differ"].items():
            for op in ops:
                differ.setdefault(site, {}).setdefault(op, set()).add(n)
    missing = [t["id"] for t in tests if t["number"] not in seen_c]
    if missing:
        sys.exit(f"REFUSING TO BUILD: {len(missing)} tests have no comparison data, e.g. {missing[:3]}")
    if tainted_c:
        sys.exit(f"REFUSING TO BUILD: {len(tainted_c)} tests changed global state while probing, e.g. {sorted(tainted_c)[:3]}")
    # every sqlglot module is loaded through the profiler, so every sqlglot file counts
    cmp_files_out = sorted(r for r in line_tests if r.startswith("sqlglot/"))
    cmp_differ_out = {site: {op: sid(ts) for op, ts in ops.items()} for site, ops in differ.items()}
    print(f"comparisons: {len(differ)} sites ever disagree with a partner; files profiled {len(cmp_files_out)}",
          file=sys.stderr)

result = {
    "sets": [list(k) for k in sorted(set_ids, key=set_ids.get)],
    "files": files_out,
    "containers": containers_out,
    "cmp_files": cmp_files_out,
    "cmp_differ": cmp_differ_out,
    # keep only real repository paths (the executor compiles generated code under
    # pseudo file names, which the open/compile recorder also sees)
    "opened": {p: sid(s) for p, s in sorted(opened.items()) if os.path.exists(os.path.join(SRC, p))},
}
with gzip.open(out_path, "wt") as f:
    json.dump(result, f, separators=(",", ":"))
print(f"files {len(files_out)}, sets {len(set_ids)}, opened {len(opened)}, "
      f"classes with usage {len(usage)}, narrowable settings {sum(len(f.get('settings', [])) for f in files_out.values())}, unknown contexts {len(unknown_ctx)} {sorted(unknown_ctx)[:5]}, "
      f"size {os.path.getsize(out_path) / 1e6:.2f} MB", file=sys.stderr)
