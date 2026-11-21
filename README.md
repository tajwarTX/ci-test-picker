# ci-test-picker

Picks which tests a code change needs, using a coverage map built ahead of time.

- `selector.py` reads each patch, finds the changed lines and selects the tests that
  ran them (statement, function, class or file level), then spreads the tests over workers.
- `data/map.json.gz` is the precomputed map; `tools/mapper/` rebuilds it.
- `tools/eval.sh` runs the container locally and scores it on the sample set.
