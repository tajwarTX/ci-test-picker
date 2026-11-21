"""Label synthetic commits by running the FULL sqlglot suite on each (inside br-mapper).

  python label.py /data/tests.json /m/commits /m/answers.json JOBS
For every patch: apply it to /src, run pytest -n JOBS on everything (SKIP_INTEGRATION=0,
as the organizers labeled), record failing tests (failures, errors, failed subtests,
files that fail to import), restore /src. Writes answers.json in the sample's format.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/plugin")
tests = json.load(open(sys.argv[1]))["tests"]
commits = sorted(Path(sys.argv[2]).glob("*.patch"))
out_path = Path(sys.argv[3])
jobs = sys.argv[4]
number = {t["id"]: t["number"] for t in tests}
by_file: dict = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])

sys.path.insert(0, "/blast")
import selector as S  # reuse the diff parser / rebuild  # noqa: E402

answers = json.load(open(out_path)) if out_path.exists() else {"commits": {}}
SRC = Path("/src")
for patch in commits:
    cid = patch.stem
    if cid in answers["commits"]:
        continue
    t0 = time.time()
    changes = S.parse_patch(patch.read_text())
    saved = {}
    ok = True
    for ch in changes:
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
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", FAILS_OUT=str(res))
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", jobs, "-p", "no:cacheprovider",
                        "-p", "fail_collector", "tests"], cwd="/src", env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
        data = json.load(open(res)) if res.exists() else {"failed": [], "bad_files": ["<no result>"]}
        failing = {number[n] for n in data["failed"] if n in number}
        for bf in data["bad_files"]:
            failing |= set(by_file.get(bf, []))
        answers["commits"][cid] = {"failing": sorted(failing), "kind": cid[-1],
                                   "unmatched": [n for n in data["failed"] if n not in number][:5],
                                   "bad_files": data["bad_files"]}
    for f, old in saved.items():
        f.write_text(old)
    json.dump(answers, open(out_path, "w"), indent=0)
    print(f"{cid}: {len(answers['commits'].get(cid, {}).get('failing', []))} failing, {time.time() - t0:.0f}s",
          flush=True)
