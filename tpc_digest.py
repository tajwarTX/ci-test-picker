"""Fingerprint everything the TPC executor tests compute BEFORE executing anything.

  python tpc_digest.py OUT.json

For every TPC-H and TPC-DS fixture query it records a hash of exactly what those tests
build with sqlglot before execution starts:
  * transpile(sql, write="duckdb")            (the expected-result query run by DuckDB)
  * transpile(optimized, write="duckdb")      (test_optimized_tpch)
  * the parsed query and its table names      (sent to the worker processes)
  * optimize(..., leave_tables_isolated=True) (the tree the executor runs, with types)
  * Plan(...)                                  (every step and every field of every step)
Errors are recorded as their type, so a patch that makes a query fail differs too.

The selector runs this on the PATCHED sqlglot (in /tmp) and compares with the copy made at
build time from the pristine tree. Identical fingerprints mean the expensive execution step
receives exactly the same input as before.
"""

import hashlib
import json
import math
import os
import sys

from sqlglot import exp, find_tables, parse_one, transpile
from sqlglot.optimizer import optimize
from sqlglot.planner import Plan, Step
from sqlglot.schema import ensure_schema

sys.path.append(os.environ.get("TPC_TESTS_ROOT", "/app/src"))
from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs  # noqa: E402


def enc_expr(e):
    types = ";".join(str(n.type) if n.type is not None else "-" for n in e.walk())
    return f"X{e!r}|T{types}"


def enc(o, ids):
    if isinstance(o, exp.Expr):
        return enc_expr(o)
    if isinstance(o, Step):
        return f"S{ids.get(id(o), '?')}"
    if isinstance(o, dict):
        return "{" + ",".join(f"{enc(k, ids)}:{enc(v, ids)}" for k, v in sorted(o.items(), key=lambda kv: repr(kv[0]))) + "}"
    if isinstance(o, (list, tuple)):
        return "[" + ",".join(enc(v, ids) for v in o) + "]"
    if isinstance(o, (set, frozenset)):
        return "<" + ",".join(sorted(enc(v, ids) for v in o)) + ">"
    if isinstance(o, type):
        return f"C{o.__module__}.{o.__qualname__}"
    if isinstance(o, float) and math.isinf(o):
        return "inf"
    if o is None or isinstance(o, (str, int, float, bool)):
        return repr(o)
    return f"?{type(o).__name__}:{o!r}"


def enc_plan(plan):
    order, seen, queue = [], set(), [plan.root]
    while queue:
        s = queue.pop(0)
        if id(s) in seen:
            continue
        seen.add(id(s))
        order.append(s)
        queue.extend(sorted(s.dependencies, key=lambda d: str(d.name)))
    ids = {id(s): i for i, s in enumerate(order)}
    parts = [enc_expr(plan.expression), enc(plan.ctes, ids) if plan.ctes is not None else "-"]
    for s in order:
        fields = {k: v for k, v in vars(s).items() if k not in ("dependents",)}
        parts.append(type(s).__name__ + enc(fields, ids))
    return "\n".join(parts)


def guarded(fn):
    try:
        return fn()
    except Exception as e:  # a failure is part of the fingerprint
        return f"ERR:{type(e).__name__}"


def digest(schema_dict, fixture):
    schema = ensure_schema(schema_dict)
    out = []
    for meta, sql, optimized in load_sql_fixture_pairs(fixture):
        parts = [
            guarded(lambda: transpile(sql, write="duckdb")[0]),
            guarded(lambda: transpile(optimized, write="duckdb")[0]),
        ]
        parsed = guarded(lambda: parse_one(sql))
        if isinstance(parsed, str):
            parts.append(parsed)
        else:
            parts.append(enc_expr(parsed))
            parts.append(guarded(lambda: ",".join(sorted(t.name for t in find_tables(parsed)))))
            opt = guarded(lambda: optimize(parsed.copy(), schema, leave_tables_isolated=True))
            if isinstance(opt, str):
                parts.append(opt)
            else:
                parts.append(enc_expr(opt))
                parts.append(guarded(lambda: enc_plan(Plan(opt))))
        out.append(hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:20])
    return out


if __name__ == "__main__":
    result = {
        "tpch": digest(TPCH_SCHEMA, "optimizer/tpc-h/tpc-h.sql"),
        "tpcds": digest(TPCDS_SCHEMA, "optimizer/tpc-ds/tpc-ds.sql"),
    }
    json.dump(result, open(sys.argv[1], "w"))
