"""Synthetic comparison-operator swaps for validating the comparison rule (local only).
  python3 make_swaps.py SQLGLOT_SRC MAP OUT_DIR COUNT SEED
Half the swaps are on statements the TPC-DS executor test runs (the expensive case)."""
import ast, difflib, gzip, json, random, sys
from pathlib import Path
src, mp, out, count, seed = Path(sys.argv[1]), json.load(gzip.open(sys.argv[2], "rt")), Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
rng = random.Random(seed)
(out / "commits").mkdir(parents=True, exist_ok=True)
tests = json.load(open(Path(__file__).resolve().parents[2] / "sample/tests.json"))["tests"]
tpcds = next(t["number"] for t in tests if t["id"].endswith("test_execute_tpcds"))
PARTNER = {ast.Is: ast.Eq, ast.Eq: ast.Is, ast.IsNot: ast.NotEq, ast.NotEq: ast.IsNot,
           ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
TXT = {ast.Is: " is ", ast.Eq: " == ", ast.IsNot: " is not ", ast.NotEq: " != ", ast.Lt: " < ", ast.LtE: " <= ", ast.Gt: " > ", ast.GtE: " >= "}
sites_any, sites_tpc = [], []
for rel, info in mp["files"].items():
    if not rel.startswith("sqlglot/"):
        continue
    stmts = {l: s for l, s in info.get("stmts", [])}
    text = (src / rel).read_text()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in PARTNER \
                and node.lineno == node.end_lineno and node.lineno in stmts:
            covered = set(mp["sets"][stmts[node.lineno]])
            if covered:
                (sites_tpc if tpcds in covered else sites_any).append((rel, node))
rng.shuffle(sites_any); rng.shuffle(sites_tpc)
picked = sites_tpc[: count // 2] + sites_any[: count - count // 2]
made = 0
for i, (rel, node) in enumerate(picked):
    text = (src / rel).read_text(); lines = text.split("\n")
    line = lines[node.lineno - 1]
    a, b = TXT[type(node.ops[0])], TXT[PARTNER[type(node.ops[0])]]
    seg_start = node.left.end_col_offset
    seg_end = node.comparators[0].col_offset
    seg = line[seg_start:seg_end]
    if seg.strip() != a.strip():
        continue
    new_line = line[:seg_start] + b + line[seg_end:].lstrip() if False else line[:seg_start] + b + line[seg_end:]
    new = "\n".join(lines[: node.lineno - 1] + [new_line] + lines[node.lineno:])
    try:
        compile(new, rel, "exec")
    except SyntaxError:
        continue
    d = difflib.unified_diff(lines, new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    tag = "t" if (rel, node) in sites_tpc[: count // 2] else "a"
    (out / "commits" / f"s{i:03d}{tag}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + "\n".join(d) + "\n")
    made += 1
print(f"{made} swap patches ({len(sites_tpc)} TPC-DS-covered sites, {len(sites_any)} other sites available)")
