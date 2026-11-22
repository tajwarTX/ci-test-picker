"""Build data/tpc_map.json.gz for the TPC-DS probe (laptop; local only).

  python3 build_tpc_map.py TPCCOV_DIR ISO_DIR TESTS_JSON OUT

  base:       {rel: [lines]}  run by imports + table setup (a change there: no probe)
  q:          {rel: [[line, query bitmask], ...]}  lines each query's path runs
  unmodeled:  {rel: [lines]}  lines the real test_execute_tpcds ran (isolated coverage of
              that test, all its processes) that neither setup nor any query explains:
              a change touching one is never probed
"""
import glob, gzip, json, os, sqlite3, sys
from coverage.numbits import numbits_to_nums

cov_dir, iso_dir, tests_json, out = sys.argv[1:5]
TPC = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
base = {rel: set(ls) for rel, ls in json.load(open(os.path.join(cov_dir, "baseline.json"))).items()}
q = {}
n = 0
for f in sorted(glob.glob(os.path.join(cov_dir, "q*.json"))):
    i = int(os.path.basename(f)[1:4])
    n += 1
    for rel, ls in json.load(open(f)).items():
        d = q.setdefault(rel, {})
        for l in ls:
            d[l] = d.get(l, 0) | (1 << i)
if n != 99:
    sys.exit(f"REFUSING: {n} query coverage files, expected 99")
real = {}
for path in glob.glob(os.path.join(iso_dir, ".coverage*")):
    con = sqlite3.connect(path)
    ctx = {cid for cid, c in con.execute("select id, context from context") if c and c.removeprefix("C|") == TPC}
    if not ctx:
        continue
    fname = dict(con.execute("select id, path from file"))
    for fid, cid, bits in con.execute("select file_id, context_id, numbits from line_bits"):
        if cid in ctx:
            rel = os.path.relpath(fname[fid], "/src")
            if rel.startswith("sqlglot/"):
                real.setdefault(rel, set()).update(numbits_to_nums(bits))
if not real:
    sys.exit("REFUSING: no isolated coverage found for test_execute_tpcds")
unmodeled = {}
for rel, ls in real.items():
    extra = ls - base.get(rel, set()) - set(q.get(rel, {}))
    if extra:
        unmodeled[rel] = sorted(extra)
# execution phase (Plan + PythonExecutor + result shipping), per executed query, measured
# in fresh processes; plus every line of executor.execute() itself, for every query
executed = [int(x) for x in open(os.path.join(cov_dir, "executed.txt")).read().split()]
e = {}
n_e = 0
for i in executed:
    f = os.path.join(cov_dir, f"e{i:03d}.json")
    if not os.path.exists(f):
        sys.exit(f"REFUSING: no execution-phase coverage for query {i}")
    n_e += 1
    for rel, ls in json.load(open(f)).items():
        d = e.setdefault(rel, {})
        for l in ls:
            d[l] = d.get(l, 0) | (1 << i)
import ast as _ast
_src = open("/src/sqlglot/executor/__init__.py").read()
_fn = next(n for n in _ast.walk(_ast.parse(_src)) if isinstance(n, _ast.FunctionDef) and n.name == "execute")
all_exec = sum(1 << i for i in executed)
# warm execution stage (one process, every executed query): required
ew = os.path.join(cov_dir, "ewarm.json")
if not os.path.exists(ew):
    sys.exit("REFUSING: no warm execution-stage coverage (ewarm.json)")
for rel, ls in json.load(open(ew)).items():
    dd = e.setdefault(rel, {})
    for l in ls:
        dd[l] = dd.get(l, 0) | sum(1 << i for i in executed)
d = e.setdefault("sqlglot/executor/__init__.py", {})
for l in range(_fn.lineno, _fn.end_lineno + 1):
    d[l] = d.get(l, 0) | all_exec
# classes the execution stage uses (fresh per-query runs + one warm run, MRO included), and
# every identifier in the statements it runs plus in those classes' own bodies
eu = os.path.join(cov_dir, "euse.json")
if not os.path.exists(eu):
    sys.exit("REFUSING: no execution-stage class usage (euse.json)")
euse = sorted(json.load(open(eu)))
names = set()
def _names(node):
    for n in _ast.walk(node):
        if isinstance(n, _ast.Name):
            names.add(n.id)
        elif isinstance(n, _ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, _ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            names.add(n.value)
for rel, dd in e.items():
    path = os.path.join("/src", rel)
    if not os.path.exists(path):
        continue
    tree = _ast.parse(open(path).read())
    lines = set(dd)
    for st_ in _ast.walk(tree):
        if isinstance(st_, _ast.stmt) and st_.lineno in lines:
            _names(st_)
by_mod = {}
for k in euse:
    mod, qual = k.split(":", 1)
    by_mod.setdefault(mod, set()).add(qual)
for mod, quals in by_mod.items():
    rel = mod.replace(".", "/") + ".py"
    path = os.path.join("/src", rel)
    if not os.path.exists(path):
        path = os.path.join("/src", mod.replace(".", "/"), "__init__.py")
        if not os.path.exists(path):
            continue
    tree = _ast.parse(open(path).read())
    def walk(stmts, prefix):
        for st in stmts:
            if isinstance(st, _ast.ClassDef):
                q = prefix + st.name
                if q in quals:
                    _names(st)  # bases, decorators and the whole body
                walk(st.body, q + ".")
    walk(tree.body, "")
# dynamic execution-stage attribute names and whole-namespace reads (edyn runs: warm "all"
# plus one fresh run per executed query); all are required
edyn_names, edyn_whole = set(), set()
for qname in ["all"] + [str(i) for i in executed]:
    fpath = os.path.join(cov_dir, f"edyn_{qname}.json")
    if not os.path.exists(fpath):
        sys.exit(f"REFUSING: missing {fpath}")
    dyn = json.load(open(fpath))
    edyn_names.update(dyn["names"])
    edyn_whole.update(dyn["whole_types"])
res = {"base": {r: sorted(v) for r, v in base.items()},
       "euse": euse, "euse_names": sorted(names),
       "edyn_names": sorted(edyn_names), "edyn_whole": sorted(edyn_whole),
       "e": {r: sorted([l, m] for l, m in dd.items()) for r, dd in e.items()},
       "executed": executed,
       "q": {r: sorted([l, m] for l, m in d.items()) for r, d in q.items()},
       "unmodeled": unmodeled}
with gzip.open(out, "wt") as f:
    json.dump(res, f, separators=(",", ":"))
tot_real = sum(len(v) for v in real.values())
print(f"execution-phase coverage for {n_e} queries; queries {n}; real TPC-DS lines {tot_real}; unmodeled {sum(len(v) for v in unmodeled.values())} "
      f"in {len(unmodeled)} files; size {os.path.getsize(out)/1e6:.2f} MB")
