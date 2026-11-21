"""Generate synthetic commits shaped like the sample (local validation only, never shipped).

  python3 make_mutants.py SQLGLOT_SRC OUT_DIR COUNT SEED
Writes OUT_DIR/commits/<id>.patch. Kinds, roughly in the sample's mix:
  code    - one small edit in sqlglot/: operator, constant, boolean, index, string
  setting - one edit inside a class-level assignment (settings dicts, flags)
  fixture - one edited line in tests/fixtures/optimizer/*.sql
  comment - one added '#' comment line in sqlglot/
  multi   - two or three of the above in different files
"""

import difflib
import random
import re
import sys
from pathlib import Path

src, out, count, seed = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
(out / "commits").mkdir(parents=True, exist_ok=True)

PY = [p for p in sorted((src / "sqlglot").rglob("*.py")) if "__main__" not in p.name]
FIX = sorted((src / "tests/fixtures/optimizer").glob("*.sql"))

SWAPS = [
    (r" is not None", " is None"), (r" is None", " is not None"), (r" is ", " == "),
    (r" == ", " != "), (r" != ", " == "), (r" and ", " or "), (r" or ", " and "),
    (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r"\bnot ", ""),
    (r"\b0\b", "1"), (r"\b1\b", "2"), (r"\b2\b", "3"), (r"\+ 1\b", "- 1"), (r"- 1\b", "+ 1"),
    (r'"([A-Z_]{3,})"', r'"\1X"'), (r"'([a-z_]{3,})'", r"'\1x'"),
]


def mutate_line(line):
    if line.strip().startswith("#") or not line.strip():
        return None
    options = [(a, b) for a, b in SWAPS if re.search(a, line)]
    rng.shuffle(options)
    for a, b in options:
        matches = list(re.finditer(a, line))
        m = rng.choice(matches)
        new = line[: m.start()] + re.sub(a, b, m.group(0), count=1) + line[m.end():]
        if new != line:
            return new
    return None


def in_function(lines, idx):
    """Crude: is line idx indented under a def?"""
    ind = len(lines[idx]) - len(lines[idx].lstrip())
    for j in range(idx - 1, -1, -1):
        l = lines[j]
        if not l.strip():
            continue
        i = len(l) - len(l.lstrip())
        if i < ind:
            if l.lstrip().startswith(("def ", "async def ")):
                return True
            if l.lstrip().startswith("class "):
                return False
            ind = i
    return False


def edit_py(kind):
    for _ in range(500):
        f = rng.choice(PY)
        text = f.read_text()
        lines = text.split("\n")
        idx = rng.randrange(len(lines))
        if kind == "comment":
            ind = re.match(r"\s*", lines[idx]).group(0)
            if not lines[idx].strip():
                continue
            new_lines = lines[:idx] + [ind + "# " + rng.choice(["Note", "TODO: revisit", "Kept for compatibility", "See above"])] + lines[idx:]
        else:
            want_func = kind == "code"
            if in_function(lines, idx) != want_func:
                continue
            new = mutate_line(lines[idx])
            if new is None:
                continue
            new_lines = lines[:idx] + [new] + lines[idx + 1:]
        new_text = "\n".join(new_lines)
        try:
            compile(new_text, str(f), "exec")
        except SyntaxError:
            continue
        return f.relative_to(src).as_posix(), text, new_text
    raise RuntimeError("no mutation found")


def edit_fixture():
    for _ in range(500):
        f = rng.choice(FIX)
        text = f.read_text()
        lines = text.split("\n")
        idx = rng.randrange(len(lines))
        l = lines[idx]
        if not l.strip() or l.startswith("#"):
            continue
        new = mutate_line(" " + l + " ")
        if new is None:
            words = l.split(" ")
            if len(words) < 3:
                continue
            j = rng.randrange(len(words))
            words[j] = words[j] + "x"
            new = " ".join(words)
        else:
            new = new[1:-1]
        if new == l:
            continue
        return f.relative_to(src).as_posix(), text, "\n".join(lines[:idx] + [new] + lines[idx + 1:])
    raise RuntimeError("no fixture edit")


def diff(rel, old, new):
    d = difflib.unified_diff(old.split("\n"), new.split("\n"), f"a/{rel}", f"b/{rel}", lineterm="", n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "\n".join(d) + "\n"


KINDS = ["code"] * 9 + ["setting"] * 5 + ["fixture"] * 8 + ["comment"] * 3 + ["multi"] * 3
for n in range(count):
    kind = rng.choice(KINDS)
    parts = []
    picks = [kind] if kind != "multi" else rng.sample(["code", "setting", "fixture", "comment"], rng.choice([2, 3]))
    used = set()
    for k in picks:
        for _ in range(20):
            rel, old, new = edit_fixture() if k == "fixture" else edit_py(k)
            if rel not in used:
                used.add(rel)
                parts.append(diff(rel, old, new))
                break
    cid = f"m{n:03d}{kind[0]}"
    (out / "commits" / f"{cid}.patch").write_text("".join(parts))
print(f"wrote {count} patches to {out}/commits")
