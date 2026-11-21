"""TPC-DS executor probe: decide whether tests/test_executor.py::test_execute_tpcds can
be affected by a change, by re-running ONLY the queries whose code paths execute the
changed statements, on the patched sqlglot, and comparing with the pristine results.

Per query, test_execute_tpcds does (main process / spawned worker):
    e = parse_one(sql)                         main
    pickle round trip of e (and the tables)    main -> worker
    if meta["execute"]:
        tables = {t.name: ... for t in find_tables(e)}
        res = execute(e, schema=TPCDS_SCHEMA, tables=tables)     worker
        duck = transpile(sql, write="duckdb")[0]                 main (run by DuckDB)
        compare res.columns/res.rows with DuckDB's result
This script runs exactly those steps and hashes (columns, rows, duck). If every
re-run query hashes the same as on the pristine commit (where the test passes), the
test passes on the patched code too.

Modes (sqlglot is imported from PYTHONPATH):
  build OUT_DIR             pristine: tables -> OUT_DIR/rows.pickle, hashes + CPU per
                            query -> OUT_DIR/pristine.json
  check OUT_DIR             re-run (another PYTHONHASHSEED); mark queries whose result
                            differs as unstable; keep the larger CPU time
  probe DATA_DIR Q [Q ...]  exit 0 if every listed query matches, 1 at the first one
                            that does not (or raises)
"""

import ast
import csv
import gzip
import hashlib
import json
import os
import pickle
import sys
import time

SRC = os.environ.get("TPC_SRC", "/app/src")
DIR = os.path.join(SRC, "tests", "fixtures", "optimizer", "tpc-ds")


def _helpers():
    sys.path.insert(0, SRC)  # tests/helpers.py: TPCDS_SCHEMA and the fixture loader
    try:
        from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs
    finally:
        sys.path.pop(0)
    return TPCDS_SCHEMA, load_sql_fixture_pairs


def _read_rows(table):
    """Rows exactly as TestExecutor.setUpClass builds them."""
    name = os.path.join(DIR, f"{table}.csv.gz")
    with open(name, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    fh = gzip.open(name, "rt", newline="") if gz else open(name, encoding="utf-8", newline="")
    reader = csv.reader(fh, delimiter="|")
    rows, ctypes = [], []
    next(reader)
    for row in reader:
        if not ctypes:
            for v in row:
                try:
                    ctypes.append(type(ast.literal_eval(v)))
                except (ValueError, SyntaxError):
                    ctypes.append(str)
        rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    return rows


class _Tables(dict):
    """Built on first use: Table(columns=..., rows=...) then a pickle round trip, as the
    test builds them in setUpClass and ships them to the worker processes."""

    def __init__(self, schema, rows):
        super().__init__()
        self.schema, self.rows = schema, rows

    def __missing__(self, name):
        from sqlglot.executor.table import Table

        t = pickle.loads(pickle.dumps(Table(columns=self.schema[name], rows=self.rows[name])))
        self[name] = t
        return t


def _digest(meta, sql, schema, tables):
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute

    e = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        return "parsed"
    used = {t.name: tables[t.name] for t in find_tables(e)}
    res = execute(e, schema=schema, tables=used)
    duck = transpile(sql, write="duckdb")[0]
    return hashlib.sha256(repr((res.columns, res.rows, duck)).encode()).hexdigest()


def main():
    mode = sys.argv[1]
    schema, load_pairs = _helpers()
    sqls = list(load_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    if mode == "build":
        out = sys.argv[2]
        rows = {t: _read_rows(t) for t in schema}
        with open(os.path.join(out, "rows.pickle"), "wb") as f:
            pickle.dump(rows, f, protocol=pickle.HIGHEST_PROTOCOL)
        tables = _Tables(schema, rows)
        res = []
        for meta, sql, _ in sqls:
            t0 = time.process_time()
            d = _digest(meta, sql, schema, tables)
            res.append({"digest": d, "cpu": round(time.process_time() - t0, 4)})
        json.dump({"queries": res}, open(os.path.join(out, "pristine.json"), "w"))
        print(f"{len(res)} queries, {sum(r['cpu'] for r in res):.1f}s CPU", file=sys.stderr)
        return 0
    if mode == "check":
        # second pristine run (other hash seed): a query whose result differs between
        # the two runs is marked unstable and never trusted by the selector
        out = sys.argv[2]
        doc = json.load(open(os.path.join(out, "pristine.json")))
        rows = pickle.load(open(os.path.join(out, "rows.pickle"), "rb"))
        tables = _Tables(schema, rows)
        bad = 0
        for (meta, sql, _), r in zip(sqls, doc["queries"]):
            t0 = time.process_time()
            try:
                d = _digest(meta, sql, schema, tables)
            except Exception:
                d = None
            r["cpu"] = round(max(r["cpu"], time.process_time() - t0), 4)
            if d != r["digest"]:
                r["unstable"] = True
                bad += 1
        json.dump(doc, open(os.path.join(out, "pristine.json"), "w"))
        print(f"unstable queries: {bad}", file=sys.stderr)
        return 0
    if mode == "probe":
        data = sys.argv[2]
        want = json.load(open(os.path.join(data, "pristine.json")))["queries"]
        rows = pickle.load(open(os.path.join(data, "rows.pickle"), "rb"))
        tables = _Tables(schema, rows)
        if len(sqls) != len(want):
            return 1
        for q in map(int, sys.argv[3:]):
            meta, sql, _ = sqls[q]
            try:
                d = _digest(meta, sql, schema, tables)
            except Exception:
                return 1
            if d != want[q]["digest"]:
                return 1
        return 0
    return 2


def max_nodes_cheap():
    """The four cheap checks of tests/test_parser.py::TestParser::test_max_nodes, in the
    test's order, on the sqlglot from PYTHONPATH. Its two huge parses are not run: the
    selector uses this only when the change touches no line they (or imports) run."""
    from sqlglot import ParseError, exp, parse_one

    if not isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select):
        return 1
    cols = [f"col{i}" for i in range(500)]
    if not isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select):
        return 1
    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
        return 1
    except ParseError:
        pass
    if not isinstance(parse_one("SELECT 1, 2, 3"), exp.Select):
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["maxnodes"]:
        try:
            sys.exit(max_nodes_cheap())
        except Exception:
            sys.exit(1)
    sys.exit(main())
