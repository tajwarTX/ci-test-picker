"""Phase coverage of tests/test_parser.py::TestParser::test_max_nodes (mapper; local only).

  python max_cov.py OUT huge    fresh process: lines run by the two huge parses (phases 3, 4)
  python max_cov.py OUT cheap   fresh process: lines run by the four cheap checks (1, 2, 5, 6)
Writes OUT/<mode>.json = {relpath: [lines]} for files under /src/sqlglot.
"""
import json, os, sys
import coverage
out, mode = sys.argv[1], sys.argv[2]
cov = coverage.Coverage(source=["/src/sqlglot"], data_file=None)
if mode == "huge":
    # "huge" also includes everything that runs while the test module and sqlglot are
    # imported: state built then (tries, registries) is used by the huge parses
    cov.start()
    sys.path.insert(0, "/src")
    import tests.test_parser  # noqa: F401
from sqlglot import ParseError, exp, parse_one  # noqa: E402
if mode != "huge":
    cov.start()
if mode == "huge":
    width, depth = 10000, 100
    expr = "x" + "=x" * depth
    sql = f"SELECT {','.join(expr for _ in range(width))}"
    for limit in (1_000_000, 50_000):
        try:
            parse_one(sql, max_nodes=limit)
        except ParseError:
            pass
else:
    parse_one("SELECT 1", max_nodes=1_000_000)
    parse_one(f"SELECT {','.join(f'col{i}' for i in range(500))} FROM t", max_nodes=1_000_000)
    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    except ParseError:
        pass
    parse_one("SELECT 1, 2, 3")
cov.stop()
data = cov.get_data()
json.dump({os.path.relpath(f, "/src"): sorted(data.lines(f) or []) for f in data.measured_files()},
          open(os.path.join(out, f"{mode}.json"), "w"))
