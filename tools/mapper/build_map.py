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

set_ids: dict[tuple, int] = {}


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
    files_out[rel] = {"all": sid(all_tests), "funcs": sorted(funcs), "stmts": stmts, "multi": multi}

result = {
    "sets": [list(k) for k in sorted(set_ids, key=set_ids.get)],
    "files": files_out,
    "opened": {p: sid(s) for p, s in sorted(opened.items())},
}
with gzip.open(out_path, "wt") as f:
    json.dump(result, f, separators=(",", ":"))
print(f"files {len(files_out)}, sets {len(set_ids)}, opened {len(opened)}, "
      f"unknown contexts {len(unknown_ctx)} {sorted(unknown_ctx)[:5]}, "
      f"size {os.path.getsize(out_path) / 1e6:.2f} MB", file=sys.stderr)
