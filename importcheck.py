"""Import check for patched sqlglot modules.

  python importcheck.py --baseline OUT.json     (build time)
      For every sqlglot module that imports cleanly at the pinned commit, record which
      sqlglot modules importing it loads (each in a fresh interpreter).
  python importcheck.py BASELINE.json MOD ...   (run time)
      Import every module whose import loads any of MOD. Exit 1 on any error.

At run time the selector points PYTHONPATH at a patched copy of sqlglot in /tmp, so a
class-level change that breaks loading (a bad name in a class body, a broken base
class, a metaclass error) is caught for real instead of guessed. A module whose import
never loads a changed module cannot be affected by it, so it is skipped.
"""

import importlib
import json
import os
import pkgutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

PROBE = (
    "import importlib, json, sys\n"
    "importlib.import_module(sys.argv[1])\n"
    "print(json.dumps(sorted(m for m in sys.modules if m == 'sqlglot' or m.startswith('sqlglot.'))))\n"
)


def baseline(out_path):
    import sqlglot

    names = ["sqlglot"]
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if not info.name.endswith("__main__"):
            names.append(info.name)

    def probe(name):
        p = subprocess.run([sys.executable, "-c", PROBE, name], capture_output=True, text=True,
                           timeout=120, env=os.environ)
        return name, (json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else None)

    with ThreadPoolExecutor(os.cpu_count() or 4) as pool:
        loads = {n: l for n, l in pool.map(probe, names) if l is not None}
    json.dump({"loads": loads}, open(out_path, "w"))
    print(f"{len(loads)} of {len(names)} modules import cleanly", file=sys.stderr)


def check(baseline_path, changed):
    loads = json.load(open(baseline_path))["loads"]
    changed = set(changed)
    targets = [m for m, l in sorted(loads.items()) if changed & set(l)]
    for name in targets:
        importlib.import_module(name)  # any exception -> non-zero exit


if __name__ == "__main__":
    if sys.argv[1] == "--baseline":
        baseline(sys.argv[2])
    else:
        check(sys.argv[1], sys.argv[2:])
