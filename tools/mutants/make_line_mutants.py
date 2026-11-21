"""Single-line mutants on given source lines (local only).
  python3 make_line_mutants.py SRC LINES_JSON OUT COUNT SEED PREFIX
LINES_JSON = {relpath: [lines]}; only lines inside function bodies are used."""
import ast, difflib, json, random, re, sys
from pathlib import Path
src, lines_json, out, count, seed, prefix = Path(sys.argv[1]), json.load(open(sys.argv[2])), Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]
rng = random.Random(seed)
(out / "commits").mkdir(parents=True, exist_ok=True)
SWAPS = [(r" is not None", " is None"), (r" is None", " is not None"), (r" == ", " != "), (r" != ", " == "),
         (r" and ", " or "), (r" or ", " and "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "),
         (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bnot ", ""), (r"\b0\b", "1"), (r"\b1\b", "2"),
         (r"\+ 1\b", "- 1"), (r"- 1\b", "+ 1"), (r"\[0\]", "[1]"), (r"\[-1\]", "[0]")]
cands = []
for rel, ls in lines_json.items():
    text = (src / rel).read_text(); L = text.split("\n")
    body = set()
    for n in ast.walk(ast.parse(text)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
            body.update(range(n.body[0].lineno, n.end_lineno + 1))
    for ln in ls:
        if ln in body and not L[ln - 1].strip().startswith(("#", '"', "'")):
            for pat, rep in SWAPS:
                if re.search(pat, L[ln - 1]):
                    cands.append((rel, ln, pat, rep))
rng.shuffle(cands)
made, seen = 0, set()
for rel, ln, pat, rep in cands:
    if made >= count or (rel, ln) in seen:
        continue
    L = (src / rel).read_text().split("\n")
    new_line = re.sub(pat, rep, L[ln - 1], count=1)
    new = L[: ln - 1] + [new_line] + L[ln:]
    try:
        compile("\n".join(new), rel, "exec")
    except SyntaxError:
        continue
    seen.add((rel, ln))
    d = difflib.unified_diff(L, new, f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    (out / "commits" / f"{prefix}{made:03d}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + "\n".join(d) + "\n")
    made += 1
print(f"{made} mutants from {len(cands)} candidates")
