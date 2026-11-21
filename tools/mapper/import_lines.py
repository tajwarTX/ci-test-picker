"""Lines run while importing every sqlglot module and every test module (mapper only).
  python import_lines.py OUT_GZ"""
import gzip, importlib, json, os, pkgutil, sys
import coverage
cov = coverage.Coverage(source=["/src/sqlglot"], data_file=None)
cov.start()
sys.path.insert(0, "/src")
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass
for d in ("tests", "tests/dialects"):
    for f in sorted(os.listdir(os.path.join("/src", d))):
        if f.startswith("test_") and f.endswith(".py"):
            try:
                importlib.import_module(f"{d.replace('/', '.')}.{f[:-3]}")
            except Exception:
                pass
cov.stop()
data = cov.get_data()
res = {os.path.relpath(f, "/src"): sorted(data.lines(f) or []) for f in data.measured_files()}
with gzip.open(sys.argv[1], "wt") as fh:
    json.dump(res, fh, separators=(",", ":"))
print(sum(len(v) for v in res.values()), "import-time lines in", len(res), "files")
