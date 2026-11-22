"""Build data/case_map.json.gz from case_cov.py shards (mapper image; local only).

  python build_case_map.py CASECOV_DIR OUT

Per test: the shards must cover cases[0:n] exactly, agree on the case list, and every ok
flag must hold (pre run, each single-case run, all-cases run; all-cases run saw n cases),
else the test is left out. Line coverage becomes FUNCTION level on the pristine source:
a covered line inside a function body counts as the OUTERMOST def holding it (module-level
function or method, through classes), keyed by that def's line. Covered lines outside any
function body (import-time code) are only counted in "pre_toplevel".

  {"version": 1, "tests": {test_id: {"pre_cpu", "pre_funcs": {rel: [defline]},
     "cases": [[loader, file, idx]], "cpu": [..], "funcs": {rel: {defline: [case pos]}},
     "pre_toplevel": n}}}
"""
import ast
import glob
import gzip
import json
import os
import sys

src_root = "/src"
cov_dir, out = sys.argv[1], sys.argv[2]
_spans = {}


def spans(rel):
    """Sorted (body_first, end, defline) of outermost defs in rel."""
    if rel not in _spans:
        res = []
        try:
            tree = ast.parse(open(os.path.join(src_root, rel)).read())
        except Exception:
            tree = None

        def walk(stmts):
            for st in stmts:
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if st.body:
                        res.append((st.body[0].lineno, st.end_lineno, st.lineno))
                elif isinstance(st, ast.ClassDef):
                    walk(st.body)
                else:
                    # defs nested in if/try/with at module or class level
                    for field in ("body", "orelse", "finalbody", "handlers"):
                        sub = getattr(st, field, None)
                        if isinstance(sub, list):
                            walk([x for x in sub if isinstance(x, ast.stmt)] +
                                 [y for h in sub if isinstance(h, ast.ExceptHandler) for y in h.body])

        if tree is not None:
            walk(tree.body)
        _spans[rel] = sorted(res)
    return _spans[rel]


def to_funcs(lines_by_rel):
    funcs, top = {}, 0
    for rel, lines in lines_by_rel.items():
        sp = spans(rel)
        for l in lines:
            d = next((dl for lo, hi, dl in sp if lo <= l <= hi), None)
            if d is None:
                top += 1
            else:
                funcs.setdefault(rel, set()).add(d)
    return funcs, top


by_test = {}
for f in glob.glob(os.path.join(cov_dir, "*.json")):
    d = json.load(open(f))
    if "test" in d:
        by_test.setdefault(d["test"], []).append(d)

tests_out, excluded = {}, {}
for tid, shards in sorted(by_test.items()):
    shards.sort(key=lambda d: d["lo"])
    cases = [tuple(c) for c in shards[0]["cases"]]
    n = len(cases)
    why = None
    if any([tuple(c) for c in s["cases"]] != cases for s in shards):
        why = "shards disagree on the case list"
    elif shards[0]["lo"] != 0 or shards[-1]["hi"] != n or \
            any(a["hi"] != b["lo"] for a, b in zip(shards, shards[1:])) or \
            any(len(s["case_lines"]) != s["hi"] - s["lo"] for s in shards):
        why = "shards do not cover the cases exactly"
    elif not all(all(s["ok"].values()) for s in shards):
        why = "an ok flag failed: " + json.dumps([s["ok"] for s in shards if not all(s["ok"].values())][:1])
    elif any(s["n_seen"] != n for s in shards):
        why = "all-cases run saw a different number of cases"
    if why:
        excluded[tid] = why
        continue
    pre_funcs, top = to_funcs(shards[0]["pre"])
    funcs, cpu = {}, []
    for s in shards:
        for k, lines in enumerate(s["case_lines"]):
            pos = s["lo"] + k
            fs, t = to_funcs(lines)
            top += t
            for rel, dls in fs.items():
                for dl in dls:
                    funcs.setdefault(rel, {}).setdefault(str(dl), []).append(pos)
        cpu += s["cpu"]
    tests_out[tid] = {
        "pre_cpu": max(s["pre_cpu"] for s in shards),
        "pre_funcs": {rel: sorted(v) for rel, v in pre_funcs.items()},
        "cases": [list(c) for c in cases],
        "cpu": cpu,
        "funcs": {rel: {dl: sorted(set(p)) for dl, p in v.items()} for rel, v in funcs.items()},
        "pre_toplevel": top,
    }

with gzip.open(out, "wt") as fh:
    json.dump({"version": 1, "tests": tests_out}, fh, separators=(",", ":"))
for tid, t in tests_out.items():
    print(f"{tid}: {len(t['cases'])} cases, pre_cpu {t['pre_cpu']:.3f}, case cpu sum {sum(t['cpu']):.2f} "
          f"median {sorted(t['cpu'])[len(t['cpu']) // 2]:.4f}")
for tid, w in excluded.items():
    print(f"EXCLUDED {tid}: {w}")
print(f"size {os.path.getsize(out) / 1e6:.2f} MB")
