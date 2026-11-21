"""Registry-table edits (local only): validation set for the registry narrowing.
One edit per commit on one entry line of a multi-line tracked dict/set (KEYWORDS-style
tables with the trie flag, string sets): rename a string key, delete the entry, or (dicts)
change its value.
  python3 make_reg_mutants.py SRC MAP OUT COUNT SEED"""
import difflib, gzip, json, random, re, sys
from pathlib import Path

src, mp, out, count, seed = Path(sys.argv[1]), json.load(gzip.open(sys.argv[2], "rt")), Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
rng = random.Random(seed)
(out / "commits").mkdir(parents=True, exist_ok=True)
ENTRY = re.compile(r'^(\s*)"([A-Za-z_][A-Za-z0-9_ ]*)"(\s*:\s*(TokenType\.[A-Z_]+))?,\s*$')
trie, sets = [], []
for cid, c in mp["containers"].items():
    if c["start"] == c["end"] or not c["file"].startswith("sqlglot/"):
        continue
    lines = (src / c["file"]).read_text().split("\n")
    for ln in range(c["start"] + 1, c["end"]):
        m = ENTRY.match(lines[ln - 1])
        if m and (c["kind"] == "set" or m.group(4)):
            (trie if "trie" in c else sets).append((c["file"], ln, m))
rng.shuffle(trie)
rng.shuffle(sets)
picks = trie[: count * 3 // 5] + sets[: count - count * 3 // 5]
for i, (rel, ln, m) in enumerate(picks):
    lines = (src / rel).read_text().split("\n")
    old = lines[ln - 1]
    kind = rng.choice(["rename", "delete"] + (["value"] if m.group(4) else []))
    new = list(lines)
    if kind == "rename":
        new[ln - 1] = old.replace(f'"{m.group(2)}"', f'"{m.group(2)}X"', 1)
    elif kind == "delete":
        del new[ln - 1]
    else:
        new[ln - 1] = old.replace(m.group(4), "TokenType.VAR" if m.group(4) != "TokenType.VAR" else "TokenType.STRING", 1)
    diff = difflib.unified_diff([l + "\n" for l in lines], [l + "\n" for l in new], f"a/{rel}", f"b/{rel}")
    body = "".join(diff)
    (out / "commits" / f"r{i:03d}.patch").write_text(f"diff --git a/{rel} b/{rel}\n{body}")
    print(f"r{i:03d} {kind:6} {rel}:{ln} {old.strip()}")
