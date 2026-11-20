#!/usr/bin/env bash
# Local eval: build the image, run it 3 times under the scorer's limits on the
# sample commits, and report simulated seconds + misses + container CPU time,
# using the middle of the 3 totals, the way the board does.
#   tools/eval.sh            normal (cached) build
#   tools/eval.sh --clean    docker build --no-cache (the "clean build check")
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=blast-radius-entry
NOCACHE=""
[[ "${1:-}" == "--clean" ]] && NOCACHE="--no-cache"

echo ">> building ($([[ -n $NOCACHE ]] && echo clean || echo cached))" >&2
docker build $NOCACHE --platform linux/amd64 --tag "$IMAGE" . >/tmp/br-build.log 2>&1 \
  || { tail -40 /tmp/br-build.log; echo "BUILD FAILED" >&2; exit 1; }

# Same flags as the Makefile / scorer.
FLAGS=(--rm --platform linux/amd64 --network none --cpus 2 --memory 4096m
  --memory-swap 4096m --pids-limit 256 --read-only
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp
  --volume "$PWD/sample/commits:/data/commits:ro"
  --volume "$PWD/sample/tests.json:/data/tests.json:ro")

# Run the image's own CMD, then print the container's total CPU (cgroup) to stderr.
CMD=$(docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' "$IMAGE")
totals=()
for run in 1 2 3; do
  start=$(date +%s)
  docker run "${FLAGS[@]}" "$IMAGE" sh -c "$CMD; rc=\$?; grep usage_usec /sys/fs/cgroup/cpu.stat >&2; exit \$rc" \
    >output.jsonl 2>/tmp/br-stderr.log || { tail -30 /tmp/br-stderr.log; echo "RUN CRASHED" >&2; exit 1; }
  wall=$(( $(date +%s) - start ))
  cpu_us=$(grep usage_usec /tmp/br-stderr.log | tail -1 | awk '{print $2}')
  out=$(python3 score.py sample sample/answers.json output.jsonl) || { echo "$out"; echo "REJECTED" >&2; exit 1; }
  sim=$(echo "$out" | awk '/^Simulated:/{print $2}')
  miss=$(echo "$out" | awk '/^Misses:/{print $2}')
  total=$(python3 -c "print(round($sim + $cpu_us/1e6, 3))")
  printf 'run %d: simulated %8.3f s  misses %s  cpu %6.2f s  total %8.3f s  (wall %ds)\n' \
    "$run" "$sim" "$miss" "$(python3 -c "print($cpu_us/1e6)")" "$total" "$wall"
  totals+=("$total")
done
mid=$(printf '%s\n' "${totals[@]}" | sort -g | sed -n 2p)
echo "SCORE (middle of 3): $mid s   [sample has 30 commits; starter baseline here = 1210.5 + cpu]"
echo "stderr of last run (tail):"; grep -v usage_usec /tmp/br-stderr.log | tail -5
