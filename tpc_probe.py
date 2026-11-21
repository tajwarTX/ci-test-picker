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


class _StopAtPlan(Exception):
    pass


def _fingerprint(root):
    """sha256 of everything an expression tree holds (class, every arg, comments, type,
    meta, position and any other attribute), computed WITHOUT sqlglot code, so a patched
    sqlglot cannot hide a difference. Iterative (TPC-DS trees are deep). Every stack
    entry is tagged: 0 value, 1 arg name, 2 end of node, 3 type follows."""
    h = hashlib.sha256()
    stack = [(0, root)]
    n = 0
    while stack:
        tag, x = stack.pop()
        n += 1
        if n > 20_000_000:
            raise RuntimeError("tree too large")
        if tag == 1:
            h.update(b"|k:" + repr(x).encode())
            continue
        if tag == 2:
            h.update(b">")
            continue
        if tag == 3:
            h.update(b"|t:")
            stack.append((0, x))
            continue
        t = type(x)
        if "args" in _all_slots(t) and isinstance(getattr(x, "args", None), dict):
            h.update(f"<{t.__module__}.{t.__qualname__}|{x.arg_key!r}|{x.index!r}|{x.comments!r}".encode())
            meta = x._meta
            h.update(repr(sorted((repr(k), repr(v)) for k, v in meta.items()) if meta else meta).encode())
            for name in sorted(_all_slots(t) - _EXPR_SLOTS):
                h.update(f"|{name}={getattr(x, name, None)!r}".encode())
            d = getattr(x, "__dict__", None)
            if d:
                h.update(repr(sorted((k, repr(v)) for k, v in d.items())).encode())
            stack.append((2, None))
            for k in sorted(x.args, reverse=True):
                stack.append((0, x.args[k]))
                stack.append((1, k))
            stack.append((3, x._type))
        elif t is list or t is tuple:
            h.update(f"{'[' if t is list else '('}{len(x)}".encode())
            stack.extend((0, v) for v in reversed(x))
        elif t is dict:
            h.update(f"{{{len(x)}".encode())
            for k in sorted(x, key=repr, reverse=True):
                stack.append((0, x[k]))
                stack.append((1, k))
        else:
            h.update(f"({t.__module__}.{t.__qualname__}:{x!r})".encode())
    return h.hexdigest()


_EXPR_SLOTS = {"args", "parent", "arg_key", "index", "comments", "_type", "_meta", "_hash"}


def _all_slots(cls):
    out = set()
    for c in cls.__mro__:
        sl = c.__dict__.get("__slots__", ())
        out.update((sl,) if isinstance(sl, str) else sl)
    return out


def _prep_digest(meta, sql, schema, tables):
    """What test_execute_tpcds computes BEFORE planning, for one query: the parse + pickle
    round trip; for executed queries, the real execute() run up to Plan (tables, schema,
    optimize, the eager `expression.sql(pretty=True)` of its debug log) and the
    expression it would plan, plus the DuckDB SQL. Execution itself is not run: the
    selector only uses this when no function the execution stage runs was changed."""
    from sqlglot import find_tables, parse_one, transpile
    import sqlglot.executor as ex

    e = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        return "parsed"
    used = {t.name: tables[t.name] for t in find_tables(e)}
    got = []

    def stop(expression):
        got.append(expression)
        raise _StopAtPlan()

    real = ex.Plan
    ex.Plan = stop
    try:
        ex.execute(e, schema=schema, tables=used)
    except _StopAtPlan:
        pass
    finally:
        ex.Plan = real
    if len(got) != 1:
        raise RuntimeError("execute() did not reach Plan")
    duck = transpile(sql, write="duckdb")[0]
    return hashlib.sha256((_fingerprint(got[0]) + "|" + repr(duck) + "|" + repr(sorted(used))).encode()).hexdigest()


def prep_main(mode, args):
    schema, load_pairs = _helpers()
    sqls = list(load_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    data = args[0]
    rows = pickle.load(open(os.path.join(data, "rows.pickle"), "rb"))
    tables = _Tables(schema, rows)
    path = os.path.join(data, "prep.json")
    if mode == "prep-build":
        res = []
        for meta, sql, _ in sqls:
            t0 = time.process_time()
            d = _prep_digest(meta, sql, schema, tables)
            res.append({"digest": d, "cpu": round(time.process_time() - t0, 4)})
        json.dump({"queries": res}, open(path, "w"))
        print(f"prep: {len(res)} queries, {sum(r['cpu'] for r in res):.1f}s CPU", file=sys.stderr)
        return 0
    if mode == "prep-check":
        doc = json.load(open(path))
        bad = 0
        for (meta, sql, _), r in zip(sqls, doc["queries"]):
            t0 = time.process_time()
            try:
                d = _prep_digest(meta, sql, schema, tables)
            except Exception:
                d = None
            r["cpu"] = round(max(r["cpu"], time.process_time() - t0), 4)
            if d != r["digest"]:
                r["unstable"] = True
                bad += 1
        # a second pass in the same (now warm) process: per-query cost once the one-time
        # start-up work (lazy imports, caches, tables) is done; "fixed" is that start-up
        warm_total = 0.0
        for (meta, sql, _), r in zip(sqls, doc["queries"]):
            t0 = time.process_time()
            try:
                d = _prep_digest(meta, sql, schema, tables)
            except Exception:
                d = None
            r["warm"] = round(time.process_time() - t0, 4)
            warm_total += r["warm"]
            if d != r["digest"]:
                r["unstable"] = True
                bad += 1
        doc["fixed"] = round(max(0.0, sum(r["cpu"] for r in doc["queries"]) - warm_total), 4)
        json.dump(doc, open(path, "w"))
        print(f"prep unstable queries: {bad}; cold {sum(r['cpu'] for r in doc['queries']):.1f}s "
              f"warm {warm_total:.1f}s fixed {doc['fixed']:.1f}s", file=sys.stderr)
        return 0
    if mode == "prep":
        want = json.load(open(path))["queries"]
        if len(sqls) != len(want):
            return 1
        for q in map(int, args[1:]):
            meta, sql, _ = sqls[q]
            if want[q].get("unstable"):
                return 1
            try:
                d = _prep_digest(meta, sql, schema, tables)
            except Exception:
                return 1
            if d != want[q]["digest"]:
                return 1
        return 0
    return 2


TPCH_DIR = os.path.join(SRC, "tests", "fixtures", "optimizer", "tpc-h")


def tpch_rows():
    """Rows of every TPC-H table, read exactly as TestExecutor.setUpClass reads them
    (plain Python: csv + ast.literal_eval, no sqlglot code)."""
    global DIR
    sys.path.insert(0, SRC)
    try:
        from tests.helpers import TPCH_SCHEMA
    finally:
        sys.path.pop(0)
    saved, DIR = DIR, TPCH_DIR
    try:
        return {t: _read_rows(t) for t in TPCH_SCHEMA}
    finally:
        DIR = saved


def _tpch_setup(data_dir):
    """What TestExecutor.setUpClass does with sqlglot: a Table for every TPC-H and TPC-DS
    table (rows prepared at build time, see tpch_rows/_read_rows) and both fixture files."""
    # the sqlglot imports of tests/test_executor.py (so their import-time code runs too)
    from sqlglot import exp, find_tables, parse_one, transpile  # noqa: F401
    from sqlglot.errors import ExecuteError  # noqa: F401
    from sqlglot.executor import env, execute  # noqa: F401
    from sqlglot.executor.python import Python, PythonExecutor  # noqa: F401
    from sqlglot.executor.table import Table, ensure_tables  # noqa: F401
    from sqlglot.optimizer import optimize  # noqa: F401
    from sqlglot.planner import Plan  # noqa: F401

    sys.path.insert(0, SRC)
    try:
        from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs
    finally:
        sys.path.pop(0)
    hrows = pickle.load(open(os.path.join(data_dir, "rows_tpch.pickle"), "rb"))
    drows = pickle.load(open(os.path.join(data_dir, "rows.pickle"), "rb"))
    tpch = {t: Table(columns=c, rows=hrows[t]) for t, c in TPCH_SCHEMA.items()}
    for t, c in TPCDS_SCHEMA.items():
        Table(columns=c, rows=drows[t])
    pickle.loads(pickle.dumps(tpch))  # the tables go to the pool workers by pickle
    pairs = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
    list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    return TPCH_SCHEMA, tpch, pairs


def tpch_digests(data_dir):
    """{"exec": [...], "opt": [...]}: per TPC-H fixture pair, the digest of what
    test_execute_tpch computes with sqlglot (parse + pickle round trip, and execution
    for pairs marked execute), and of what test_optimized_tpch computes with sqlglot
    (the DuckDB SQL of the query and of its expected optimized form)."""
    from sqlglot import transpile

    schema, real, pairs = _tpch_setup(data_dir)
    tables = {k: pickle.loads(pickle.dumps(v)) for k, v in real.items()}  # as in the workers
    ex, op = [], []
    for meta, sql, optimized in pairs:
        ex.append(_digest(meta, sql, schema, tables))
        op.append(hashlib.sha256(repr((transpile(sql, write="duckdb")[0],
                                       transpile(optimized, write="duckdb")[0])).encode()).hexdigest())
    return {"exec": ex, "opt": op}


def generator_memory():
    """tests/test_generator.py::TestGenerator::test_generator_memory, exactly."""
    import gc
    import tracemalloc

    sys.path.insert(0, SRC)
    try:
        import tests.test_generator  # noqa: F401  (the test module's own imports)
    finally:
        sys.path.pop(0)
    from sqlglot.dialects.dialect import Dialect, Dialects

    for name in Dialects:
        dialect = Dialect.get_or_raise(name)
        for _ in range(100):
            dialect.generator()
        gc.collect()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(1000):
                dialect.generator()
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        if not after - before < 1024:
            return 1
    return 0


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
    if sys.argv[1:2] == ["tpch-build"]:
        pickle.dump(tpch_rows(), open(os.path.join(sys.argv[2], "rows_tpch.pickle"), "wb"),
                    protocol=pickle.HIGHEST_PROTOCOL)
        json.dump(tpch_digests(sys.argv[2]), open(os.path.join(sys.argv[2], "tpch.json"), "w"))
        sys.exit(0)
    if sys.argv[1:2] == ["tpch"]:
        # tpch DATA_DIR exec|opt [exec|opt]: 0 if the named test's sqlglot results match
        try:
            want = json.load(open(os.path.join(sys.argv[2], "tpch.json")))
            got = tpch_digests(sys.argv[2])
            sys.exit(0 if all(got[k] == want[k] for k in sys.argv[3:]) else 1)
        except Exception:
            sys.exit(1)
    if sys.argv[1:2] == ["genmem"]:
        try:
            sys.exit(generator_memory())
        except Exception:
            sys.exit(1)
    if sys.argv[1] in ("prep-build", "prep-check", "prep"):
        try:
            sys.exit(prep_main(sys.argv[1], sys.argv[2:]))
        except Exception:
            sys.exit(1)
    if sys.argv[1:2] == ["maxnodes"]:
        try:
            sys.exit(max_nodes_cheap())
        except Exception:
            sys.exit(1)
    sys.exit(main())
