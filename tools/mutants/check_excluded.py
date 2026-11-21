"""Safety check for narrowing (runs inside br-mapper; local validation only).

  python check_excluded.py /data/tests.json /m/plan.json /m/result.json JOBS
plan.json: {commit_id: {"patch": "...", "run": [test ids]}} - tests the selector LEFT OUT
(cheap ones only). Applies each patch to /src, runs those tests, and reports any that
fail: each one is a miss the narrowing would have caused.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/blast")
import selector as S  # noqa: E402

plan = json.load(open(sys.argv[2]))
out_path = Path(sys.argv[3])
jobs = sys.argv[4]
result = json.load(open(out_path)) if out_path.exists() else {}
SRC = Path("/src")
for cid, item in sorted(plan.items()):
    if cid in result or not item["run"]:
        continue
    t0 = time.time()
    saved = {}
    ok = True
    for ch in S.parse_patch(item["patch"]):
        f = SRC / ch.old
        old = f.read_text()
        new = S.rebuild(old, ch.hunks)
        if new is None:
            ok = False
            break
        saved[f] = old
        f.write_text(new)
    if ok:
        res = Path("/tmp/fails.json")
        res.unlink(missing_ok=True)
        ids = Path("/tmp/ids.txt")
        ids.write_text("\n".join(item["run"]))
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", FAILS_OUT=str(res),
                   PYTHON_CPU_COUNT="2")
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", jobs, "-p", "no:cacheprovider",
                        "-p", "fail_collector", *item["run"]], cwd="/src", env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
        data = json.load(open(res)) if res.exists() else {"failed": ["<no result>"], "bad_files": []}
        result[cid] = {"ran": len(item["run"]), "failed": data["failed"], "bad_files": data["bad_files"]}
    for f, old in saved.items():
        f.write_text(old)
    json.dump(result, open(out_path, "w"), indent=0)
    r = result.get(cid, {})
    print(f"{cid}: ran {r.get('ran')} excluded tests, FAILED {len(r.get('failed', []))} "
          f"{r.get('failed', [])[:3]} bad_files {r.get('bad_files')} ({time.time() - t0:.0f}s)", flush=True)
