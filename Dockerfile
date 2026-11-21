# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Pristine sqlglot at the pinned commit (allowed by the rules): the selector applies
# each patch to it to check the changed files still compile and import.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/src \
 && tar xzf /tmp/sqlglot.tar.gz -C /app/src --strip-components=1 --wildcards '*.py' \
 && rm /tmp/sqlglot.tar.gz \
 && test -f /app/src/sqlglot/parser.py

# Bytecode for the pristine tree (so run-time import checks only compile patched
# files) and the list of modules that import cleanly at the pinned commit.
COPY importcheck.py /app/importcheck.py
RUN python -m compileall -q /app/src/sqlglot \
 && PYTHONPATH=/app/src python /app/importcheck.py --baseline /app/import_ok.json \
 && test -s /app/import_ok.json

# Coverage/class-usage map built from the pinned commit (tools/mapper), committed as a plain file.
COPY selector.py /app/selector.py
COPY data/map.json.gz /app/map.json.gz
RUN chmod -R a+rX /app

CMD ["python", "/app/selector.py"]
