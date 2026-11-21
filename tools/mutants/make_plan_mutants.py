"""Code mutants on lines that test_execute_tpcds runs, inside function bodies (local only):
validation set for the TPC-DS plan check: only lines whose outermost function the execution
stage never runs.   python3 make_plan_mutants.py SRC MAP TPC_MAP OUT COUNT SEED"""
import ast, difflib, gzip, json, random, re, sys
from pathlib import Path
src, mp, out, count, seed = Path(sys.argv[1]), json.load(gzip.open(sys.argv[2], "rt")), Path(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6])
E = {r: {l for l, _ in v} for r, v in json.load(gzip.open(sys.argv[3], "rt"))["e"].items()}
rng = random.Random(seed)
(out / "commits").mkdir(parents=True, exist_ok=True)
tests = json.load(open(Path(__file__).resolve().parents[2] / "sample/tests.json"))["tests"]
tpc = next(t["number"] for t in tests if t["id"].endswith("test_execute_tpcds"))
SWAPS = [(r" is not None", " is None"), (r" is None", " is not None"), (r" == ", " != "), (r" != ", " == "),
         (r" and ", " or "), (r" or ", " and "), (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "),
         (r" >= ", " > "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bnot ", ""),
         (r"\b0\b", "1"), (r"\b1\b", "2"), (r"\+ 1\b", "- 1"), (r"- 1\b", "+ 1"), (r"\[0\]", "[1]"), (r"\[-1\]", "[0]")]
cands = []
for rel, info in mp["files"].items():
    if not rel.startswith("sqlglot/"):
        continue
    lines = (src / rel).read_text().split("\n")
    tree = ast.parse("\n".join(lines))
    body_lines = set()
    def outer(stmts):
        for s in stmts:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield s
            elif isinstance(s, ast.ClassDef):
                yield from outer(s.body)
    for n in outer(tree.body):
        if n.body and not any(n.lineno <= l <= n.end_lineno for l in E.get(rel, ())):
            body_lines.update(range(n.body[0].lineno, n.end_lineno + 1))
    for first, sid in info.get("stmts", []):
        if first in body_lines and tpc in set(mp["sets"][sid]):
            line = lines[first - 1]
            if line.strip().startswith(("#", '"', "'")):
                continue
            for pat, rep in SWAPS:
                if re.search(pat, line):
                    cands.append((rel, first, pat, rep))
rng.shuffle(cands)
made, seen = 0, set()
for rel, ln, pat, rep in cands:
    if made >= count or (rel, ln) in seen:
        continue
    lines = (src / rel).read_text().split("\n")
    new_line = re.sub(pat, rep, lines[ln - 1], count=1)
    if new_line == lines[ln - 1]:
        continue
    new = lines[: ln - 1] + [new_line] + lines[ln:]
    try:
        compile("\n".join(new), rel, "exec")
    except SyntaxError:
        continue
    seen.add((rel, ln))
    d = difflib.unified_diff(lines, new, f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    (out / "commits" / f"p{made:03d}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + "\n".join(d) + "\n")
    made += 1
print(f"{made} plan-check-eligible TPC-DS mutants from {len(cands)} candidates")
