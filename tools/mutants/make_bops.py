"""Synthetic `and` <-> `or` swaps for validating the and/or rule (local only).
  python3 make_bops.py SQLGLOT_SRC MAP OUT_DIR COUNT SEED
Half are on statements the TPC-DS executor test runs (the expensive case)."""
import ast, difflib, gzip, json, random, sys
from pathlib import Path
src, mp, out, count, seed = Path(sys.argv[1]), json.load(gzip.open(sys.argv[2], "rt")), Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
rng = random.Random(seed)
(out / "commits").mkdir(parents=True, exist_ok=True)
tests = json.load(open(Path(__file__).resolve().parents[2] / "sample/tests.json"))["tests"]
tpcds = next(t["number"] for t in tests if t["id"].endswith("test_execute_tpcds"))
sites_any, sites_tpc = [], []
for rel, info in mp["files"].items():
    if not rel.startswith("sqlglot/"):
        continue
    stmts = {l: s for l, s in info.get("stmts", [])}
    text = (src / rel).read_text()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.BoolOp) and len(node.values) == 2:
            a, b = node.values
            if a.end_lineno != b.lineno:
                continue
            ln = a.end_lineno
            for l in range(node.lineno, ln + 1):
                if l in stmts:
                    covered = set(mp["sets"][stmts[l]])
                    if covered:
                        (sites_tpc if tpcds in covered else sites_any).append((rel, node))
                    break
rng.shuffle(sites_any); rng.shuffle(sites_tpc)
picked = sites_tpc[: count // 2] + sites_any[: count - count // 2]
made = 0
for i, (rel, node) in enumerate(picked):
    lines = (src / rel).read_text().split("\n")
    a, b = node.values
    line = lines[a.end_lineno - 1]
    seg = line[a.end_col_offset:b.col_offset]
    word = "and" if isinstance(node.op, ast.And) else "or"
    if seg.strip() != word:
        continue
    new_line = line[:a.end_col_offset] + seg.replace(word, "or" if word == "and" else "and") + line[b.col_offset:]
    new = "\n".join(lines[: a.end_lineno - 1] + [new_line] + lines[a.end_lineno:])
    try:
        compile(new, rel, "exec")
    except SyntaxError:
        continue
    d = difflib.unified_diff(lines, new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    tag = "t" if i < count // 2 else "a"
    (out / "commits" / f"b{i:03d}{tag}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + "\n".join(d) + "\n")
    made += 1
print(f"{made} and/or patches ({len(sites_tpc)} TPC-DS-covered sites, {len(sites_any)} other)")
