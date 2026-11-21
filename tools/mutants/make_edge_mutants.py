"""Edits of one entry value in one-line tracked dicts/sets at class/module level
(local only): validation set for the brace-confined entry rule.
  python3 make_edge_mutants.py SRC MAP OUT COUNT SEED"""
import ast, difflib, gzip, json, random, re, sys
from pathlib import Path
src, mp, out, count, seed = Path(sys.argv[1]), json.load(gzip.open(sys.argv[2], "rt")), Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
rng = random.Random(seed)
(out / "commits").mkdir(parents=True, exist_ok=True)
EDITS = [(r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\b(\d+)\b", lambda m: str(int(m.group(1)) + 1)),
         (r'"([A-Za-z_]+)"', lambda m: f'"{m.group(1)}X"')]
cands = []
for cid, c in mp["containers"].items():
    if c["start"] != c["end"] or not c["file"].startswith("sqlglot/"):
        continue
    rel, line, col = cid.rsplit(":", 2)
    cands.append((rel, int(line), int(col)))
rng.shuffle(cands)
made = 0
for rel, line, col in cands:
    if made >= count:
        break
    lines = (src / rel).read_text().split("\n")
    text = lines[line - 1]
    node = next((n for n in ast.walk(ast.parse("\n".join(lines))) if isinstance(n, (ast.Dict, ast.Set))
                 and n.lineno == line and n.col_offset == col), None)
    if node is None:
        continue
    vals = node.values if isinstance(node, ast.Dict) else node.elts
    vals = [v for v in vals if v is not None and v.lineno == v.end_lineno == line]
    if not vals:
        continue
    v = rng.choice(vals)
    seg = text[v.col_offset:v.end_col_offset]
    pat, rep = rng.choice(EDITS)
    new_seg = re.sub(pat, rep, seg, count=1)
    if new_seg == seg:
        continue
    new_line = text[:v.col_offset] + new_seg + text[v.end_col_offset:]
    new = lines[: line - 1] + [new_line] + lines[line:]
    try:
        compile("\n".join(new), rel, "exec")
    except SyntaxError:
        continue
    d = difflib.unified_diff(lines, new, f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    (out / "commits" / f"e{made:03d}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + "\n".join(d) + "\n")
    made += 1
print(f"{made} one-line container edits from {len(cands)} one-line containers")
