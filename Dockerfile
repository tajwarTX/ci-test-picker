# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Pristine sqlglot at the pinned commit (allowed by the rules): the selector applies
# each patch to it to check the changed files still compile and import.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/src \
 && tar xzf /tmp/sqlglot.tar.gz -C /app/src --strip-components=1 --wildcards '*.py' '*.toml' '*.cfg' '*/tests/fixtures/*' \
 && rm /tmp/sqlglot.tar.gz \
 && test -f /app/src/sqlglot/parser.py

# Bytecode for the pristine tree (so run-time import checks only compile patched
# files) and the list of modules that import cleanly at the pinned commit.
# the tests' own third-party dependencies, pinned as in requirements.lock (PyPI): the
# runner executes real tests on the patched code
RUN pip install --no-cache-dir duckdb==2.0.0 numpy==2.5.5 pandas==3.1.1 pytz==2026.6 python-dateutil==4.0.0 tzdata==2026.6
COPY importcheck.py /app/importcheck.py
RUN python -m compileall -q /app/src/sqlglot \
 && PYTHONPATH=/app/src python /app/importcheck.py --baseline /app/import_ok.json \
 && test -s /app/import_ok.json

# Coverage/class-usage map built from the pinned commit (tools/mapper), committed as a plain file.
COPY tpc_probe.py /app/tpc_probe.py
COPY snapshot.py /app/snapshot.py
# pristine TPC-DS results for the probe (two hash seeds: unstable queries are marked)
RUN mkdir /app/tpc && cd /tmp \
 && PYTHONPATH=/app/src PYTHONHASHSEED=0 python /app/tpc_probe.py build /app/tpc \
 && PYTHONPATH=/app/src PYTHONHASHSEED=1 python /app/tpc_probe.py check /app/tpc \
 && PYTHONPATH=/app/src PYTHONHASHSEED=0 python /app/tpc_probe.py prep-build /app/tpc \
 && PYTHONPATH=/app/src PYTHONHASHSEED=1 python /app/tpc_probe.py prep-check /app/tpc \
 && PYTHONPATH=/app/src PYTHONHASHSEED=0 python /app/tpc_probe.py tpch-build /app/tpc \
 && PYTHONPATH=/app/src PYTHONHASHSEED=1 python /app/tpc_probe.py tpch /app/tpc exec opt \
 && PYTHONPATH=/app/src python /app/tpc_probe.py genmem \
 && PYTHONPATH=/app/src PYTHONHASHSEED=0 python /app/snapshot.py build /app/tpc/snap.json.gz \
 && PYTHONPATH=/app/src PYTHONHASHSEED=1 python /app/snapshot.py check /app/tpc/snap.json.gz \
 && test -s /app/tpc/pristine.json && test -s /app/tpc/prep.json && test -s /app/tpc/rows.pickle && test -s /app/tpc/tpch.json
COPY runner.py /app/runner.py
COPY selector.py /app/selector.py
COPY data/map.json.gz /app/map.json.gz
COPY data/tpc_map.json.gz /app/tpc_map.json.gz
COPY data/max_map.json.gz /app/max_map.json.gz
COPY data/grpa_map.json.gz /app/grpa_map.json.gz
COPY data/case_map.json.gz /app/case_map.json.gz
COPY data/import_lines.json.gz /app/import_lines.json.gz
# Same map in Python's binary format: loads several times faster at run time.
RUN python -c "import gzip, json, pickle; pickle.dump(json.load(gzip.open('/app/map.json.gz', 'rt')), open('/app/map.pickle', 'wb'), protocol=pickle.HIGHEST_PROTOCOL)" \
 && chmod -R a+rX /app

CMD ["python", "/app/selector.py"]
