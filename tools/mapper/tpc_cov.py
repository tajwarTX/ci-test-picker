"""Per-query coverage for the TPC-DS probe (mapper image; local only).

  python tpc_cov.py OUT_DIR baseline     lines run by imports + table setup (TPC-H and
                                         TPC-DS tables, as TestExecutor.setUpClass)
  python tpc_cov.py OUT_DIR query I      lines run by query I's path (tpc_probe._digest)
Writes OUT_DIR/<name>.json = {relpath: [lines]} for files under /src/sqlglot.
"""

import json
import os
import pickle
import sys

sys.path.insert(0, "/blast")
os.environ.setdefault("TPC_SRC", "/src")
import coverage  # noqa: E402

out, mode = sys.argv[1], sys.argv[2]
cov = coverage.Coverage(source=["/src/sqlglot"], data_file=None, branch=False)


def lines():
    data = cov.get_data()
    return {os.path.relpath(f, "/src"): sorted(data.lines(f) or []) for f in data.measured_files()}


if mode == "baseline":
    cov.start()
    # the test module's imports
    from sqlglot import exp, find_tables, parse_one, transpile  # noqa: F401
    from sqlglot.errors import ExecuteError  # noqa: F401
    from sqlglot.executor import env, execute  # noqa: F401
    from sqlglot.executor.python import Python, PythonExecutor  # noqa: F401
    from sqlglot.executor.table import Table, ensure_tables  # noqa: F401
    from sqlglot.optimizer import optimize  # noqa: F401
    from sqlglot.planner import Plan  # noqa: F401
    import tpc_probe

    schema, load_pairs = tpc_probe._helpers()
    sys.path.insert(0, "/src")
    from tests.helpers import TPCH_SCHEMA

    sys.path.pop(0)
    for sch, d in ((TPCH_SCHEMA, "tpc-h"), (schema, "tpc-ds")):
        tpc_probe.DIR = os.path.join("/src/tests/fixtures/optimizer", d)
        for t, cols in sch.items():
            pickle.loads(pickle.dumps(Table(columns=cols, rows=tpc_probe._read_rows(t))))
    list(load_pairs("optimizer/tpc-h/tpc-h.sql"))
    list(load_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    cov.stop()
    name = "baseline"
elif mode == "prep":
    # pristine: the optimized expression execute() would plan, saved for "equery"
    i = int(sys.argv[3])
    import tpc_probe
    import sqlglot.executor as ex
    from sqlglot import find_tables, parse_one

    schema, load_pairs = tpc_probe._helpers()
    meta, sql, _ = list(load_pairs("optimizer/tpc-ds/tpc-ds.sql"))[i]
    rows = {t: tpc_probe._read_rows(t) for t in schema}
    tables = tpc_probe._Tables(schema, rows)
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    used = {t.name: tables[t.name] for t in find_tables(e)}
    got = []

    def stop(expression):
        got.append(expression)
        raise tpc_probe._StopAtPlan()

    ex.Plan = stop
    try:
        ex.execute(e, schema=schema, tables=used)
    except tpc_probe._StopAtPlan:
        pass
    pickle.dump((got[0], sorted(used)), open(os.path.join(out, f"opt{i:03d}.pickle"), "wb"))
    sys.exit(0)
elif mode == "equery":
    # a FRESH process (caches cold): lines that planning + execution + shipping the
    # result back run for query I, starting from its pristine optimized expression
    i = int(sys.argv[3])
    import logging
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import ensure_tables
    from sqlglot.planner import Plan
    import tpc_probe

    schema, load_pairs = tpc_probe._helpers()
    opt, names = pickle.load(open(os.path.join(out, f"opt{i:03d}.pickle"), "rb"))
    rows = {t: tpc_probe._read_rows(t) for t in names}
    tables = tpc_probe._Tables(schema, rows)
    used = {t: tables[t] for t in names}
    cov.start()
    tables_ = ensure_tables(used)
    plan = Plan(opt)
    logging.getLogger("sqlglot").debug("Logical Plan: %s", plan)
    result = pickle.loads(pickle.dumps(PythonExecutor(tables=tables_).execute(plan)))
    _ = (result.columns, result.rows)
    cov.stop()
    name = f"e{i:03d}"
elif mode == "euse":
    # classes the EXECUTION stage uses (methods run with self/cls bound to them or a
    # subclass; whole MRO), for one executed query in a fresh process, or "all" warm
    import logging
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import ensure_tables
    from sqlglot.planner import Plan
    import tpc_probe
    import class_usage

    schema, load_pairs = tpc_probe._helpers()
    executed = [int(x) for x in open(os.path.join(out, "executed.txt")).read().split()]
    which = executed if sys.argv[3] == "all" else [int(sys.argv[3])]
    rows = {t: tpc_probe._read_rows(t) for t in schema}
    tables = tpc_probe._Tables(schema, rows)
    work = []
    for i in which:
        opt, names = pickle.load(open(os.path.join(out, f"opt{i:03d}.pickle"), "rb"))
        work.append((opt, {t: tables[t] for t in names}))
    class_usage.install()
    for opt, used in work:
        tables_ = ensure_tables(used)
        plan = Plan(opt)
        logging.getLogger("sqlglot").debug("Logical Plan: %s", plan)
        result = pickle.loads(pickle.dumps(PythonExecutor(tables=tables_).execute(plan)))
        _ = (result.columns, result.rows)
    sys.setprofile(None)
    keys = set()
    for t in list(class_usage._seen):
        for k in getattr(t, "__mro__", ()):
            mod = getattr(k, "__module__", "") or ""
            if mod.startswith("sqlglot"):
                keys.add(f"{mod}:{k.__qualname__}")
    json.dump(sorted(keys), open(os.path.join(out, f"euse_{sys.argv[3]}.json"), "w"))
    sys.exit(0)
elif mode == "ewarm":
    # ONE process running the execution stage of every executed query in turn (warm
    # caches, like a pool worker): lines only a warm run reaches join the execution map
    import logging
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import ensure_tables
    from sqlglot.planner import Plan
    import tpc_probe

    schema, load_pairs = tpc_probe._helpers()
    executed = [int(x) for x in open(os.path.join(out, "executed.txt")).read().split()]
    rows = {t: tpc_probe._read_rows(t) for t in schema}
    tables = tpc_probe._Tables(schema, rows)
    work = []
    for i in executed:
        opt, names = pickle.load(open(os.path.join(out, f"opt{i:03d}.pickle"), "rb"))
        work.append((opt, {t: tables[t] for t in names}))
    cov.start()
    for opt, used in work:
        tables_ = ensure_tables(used)
        plan = Plan(opt)
        logging.getLogger("sqlglot").debug("Logical Plan: %s", plan)
        result = pickle.loads(pickle.dumps(PythonExecutor(tables=tables_).execute(plan)))
        _ = (result.columns, result.rows)
    cov.stop()
    name = "ewarm"
else:
    i = int(sys.argv[3])
    # import what the test module imports first (that is setup, not this query)
    from sqlglot import exp, find_tables, parse_one, transpile  # noqa: F401
    from sqlglot.executor import env, execute  # noqa: F401
    from sqlglot.executor.python import Python, PythonExecutor  # noqa: F401
    from sqlglot.executor.table import Table, ensure_tables  # noqa: F401
    from sqlglot.optimizer import optimize  # noqa: F401
    from sqlglot.planner import Plan  # noqa: F401
    import tpc_probe

    schema, load_pairs = tpc_probe._helpers()
    sqls = list(load_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    rows = {t: tpc_probe._read_rows(t) for t in schema}
    tables = tpc_probe._Tables(schema, rows)
    meta, sql, _ = sqls[i]
    cov.start()
    tpc_probe._digest(meta, sql, schema, tables)
    cov.stop()
    name = f"q{i:03d}"
json.dump(lines(), open(os.path.join(out, f"{name}.json"), "w"))
