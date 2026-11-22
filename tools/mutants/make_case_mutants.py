"""Code mutants inside functions that only a few fixture cases of a case-mapped test reach
(and not its setup): validation set for reduced fixture runs (local only).
  python3 make_case_mutants.py SRC CASE_MAP OUT COUNT SEED"""
import ast, difflib, gzip, json, random, re, sys
from pathlib import Path
src, cm, out, count, seed = Path(sys.argv[1]), json.load(gzip.open(sys.argv[2], "rt"))["tests"], Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
rng = random.Random(seed)
(out / "commits").mkdir(parents=True, exist_ok=True)
SWAPS = [(r" is not None", " is None"), (r" is None", " is not None"), (r" == ", " != "), (r" != ", " == "),
         (r" and ", " or "), (r" or ", " and "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
         (r" >= ", " > "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bnot ", ""), (r"\[0\]", "[1]")]
funcs = set()
for tid, info in cm.items():
    for rel, d in info["funcs"].items():
        pre = set(info["pre_funcs"].get(rel, ()))
        for dl, cs in d.items():
            if int(dl) not in pre and 0 < len(cs) <= max(3, len(info["cases"]) // 10):
                funcs.add((rel, int(dl)))
cands = []
for rel, dl in sorted(funcs):
    lines = (src / rel).read_text().split("\n")
    fn = next((n for n in ast.walk(ast.parse("\n".join(lines))) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno == dl), None)
    if fn is None or not fn.body:
        continue
    for ln in range(fn.body[0].lineno, fn.end_lineno + 1):
        t = lines[ln - 1]
        if t.strip().startswith(("#", '"', "'")):
            continue
        for pat, rep in SWAPS:
            if re.search(pat, t):
                cands.append((rel, ln, pat, rep))
rng.shuffle(cands)
made, seen = 0, set()
for rel, ln, pat, rep in cands:
    if made >= count or (rel, ln) in seen:
        continue
    lines = (src / rel).read_text().split("\n")
    new_line = re.sub(pat, rep, lines[ln - 1], count=1)
    new = lines[: ln - 1] + [new_line] + lines[ln:]
    try:
        compile("\n".join(new), rel, "exec")
    except SyntaxError:
        continue
    seen.add((rel, ln))
    d = difflib.unified_diff(lines, new, f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    (out / "commits" / f"k{made:03d}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + "\n".join(d) + "\n")
    made += 1
print(f"{made} mutants from {len(cands)} candidates in {len(funcs)} functions")
