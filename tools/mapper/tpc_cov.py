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
