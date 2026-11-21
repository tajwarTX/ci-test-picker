"""Run every test in tests.json in its own pytest process, under the coverage plugin.

Usage (inside the br-mapper image): python run_isolated.py /data/tests.json JOBS
Output: /out/.coverage.* and /out/opened.*.json, plus /out/isolated_log.json
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

tests = json.load(open(sys.argv[1]))["tests"]
jobs = int(sys.argv[2])
only = set(sys.argv[3:])  # optional subset of ids for trials


def run(test):
    env = dict(os.environ, MAP_TEST=test["id"])
    start = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", os.environ.get("MAP_PLUGIN", "covmap_plugin"), "-q",
             "-p", "no:cacheprovider", "-x", test["id"]],
            env=env, cwd="/src", capture_output=True, text=True, timeout=1800,
        )
        rc, tail = proc.returncode, (proc.stdout[-300:] if proc.returncode else "")
    except subprocess.TimeoutExpired:
        rc, tail = -999, "TIMEOUT"
    return {"id": test["id"], "rc": rc, "secs": round(time.time() - start, 2), "tail": tail}


todo = [t for t in tests if not only or t["id"] in only]
# Longest first so the slow ones don't finish last.
todo.sort(key=lambda t: -t["seconds"])
results = []
t0 = time.time()
with ThreadPoolExecutor(jobs) as pool:
    for i, r in enumerate(pool.map(run, todo), 1):
        results.append(r)
        if i % 50 == 0 or i == len(todo):
            print(f"{i}/{len(todo)} done, {time.time() - t0:.0f}s", flush=True)
json.dump(results, open("/out/isolated_log.json", "w"), indent=1)
bad = [r for r in results if r["rc"] != 0]
print(f"finished {len(results)} tests in {time.time() - t0:.0f}s; nonzero exit: {len(bad)}")
for r in bad[:20]:
    print("  ", r["id"], r["rc"], r["tail"].strip().splitlines()[-1:] if r["tail"] else "")
