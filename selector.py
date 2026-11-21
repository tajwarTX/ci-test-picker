"""Blast Radius selector: pick the tests a patch can break, using a coverage map.

For every patch:
  * parse the unified diff into changed/inserted line positions per file
  * a change inside a function body  -> tests that ran that function
  * a change anywhere else in a .py  -> tests whose process ran ANY line of that file
    (module/class level code, signatures and decorators run at import time)
  * a .py that no longer compiles    -> same "whole file" set
  * a non-Python file                -> tests that opened that file (or listed its folder)
  * anything we don't understand     -> every test (a wasted 30 s beats a 300 s miss)
Then the chosen tests are split over the number of workers (1..8) that costs least.
"""

import gzip
import json
import re
import sys
from pathlib import Path

DATA = Path("/data")
APP = Path(__file__).resolve().parent
MAP = APP / "map.json.gz"
SRC = APP / "src"  # pristine sqlglot checkout at the pinned commit (.py files)

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def log(*args):
    print(*args, file=sys.stderr)


class FileChange:
    def __init__(self):
        self.old = None  # path before (None if added)
        self.new = None  # path after (None if deleted)
        self.points: set[int] = set()  # old line numbers touched (removed, or next to an insertion)
        self.hunks: list[tuple[int, list[str]]] = []  # (first old line, body) to rebuild the file
        self.binary = False


def parse_patch(text: str) -> list[FileChange]:
    changes: list[FileChange] = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FileChange()
            changes.append(cur)
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                cur.old, cur.new = m.group(1), m.group(2)
        elif cur is not None and line.startswith("new file mode"):
            cur.old = None
        elif cur is not None and line.startswith("deleted file mode"):
            cur.new = None
        elif cur is not None and line.startswith("Binary files"):
            cur.binary = True
        elif line.startswith("--- "):
            if cur is None:  # plain diff without a "diff --git" header
                cur = FileChange()
                changes.append(cur)
            p = line[4:].split("\t")[0]
            cur.old = None if p == "/dev/null" else re.sub(r"^a/", "", p)
        elif line.startswith("+++ ") and cur is not None:
            p = line[4:].split("\t")[0]
            cur.new = None if p == "/dev/null" else re.sub(r"^b/", "", p)
        elif cur is not None and (m := HUNK.match(line)):
            old_start = int(m.group(1))
            old_left = 1 if m.group(2) is None else int(m.group(2))
            new_left = 1 if m.group(4) is None else int(m.group(4))
            if old_left == 0:  # pure insertion after old line old_start
                old_start += 1
            body = []
            pos = old_start  # old line number of the next old line
            i += 1
            # Read exactly as many lines as the header promises.
            while i < len(lines) and (old_left > 0 or new_left > 0):
                l = lines[i]
                i += 1
                if l.startswith("\\"):  # "\ No newline at end of file"
                    continue
                tag = l[:1] or " "
                body.append(l if l else " ")
                if tag == "-":
                    cur.points.add(pos)
                    pos += 1
                    old_left -= 1
                elif tag == "+":
                    cur.points.add(pos - 1)  # inserted between old lines pos-1 and pos
                    cur.points.add(pos)
                    new_left -= 1
                else:
                    pos += 1
                    old_left -= 1
                    new_left -= 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur.hunks.append((old_start, body))
            continue
        i += 1
    return changes


def rebuild(original: str, hunks) -> "str | None":
    """Apply hunks to the original text; None if they don't line up."""
    old = original.split("\n")
    out = []
    idx = 0  # 0-based index into old
    for start, body in hunks:
        start0 = max(start - 1, 0)
        if start0 < idx:
            return None
        out.extend(old[idx:start0])
        idx = start0
        for l in body:
            tag, text = l[:1], l[1:]
            if tag == " ":
                if idx >= len(old) or old[idx] != text:
                    return None
                out.append(text)
                idx += 1
            elif tag == "-":
                if idx >= len(old) or old[idx] != text:
                    return None
                idx += 1
            elif tag == "+":
                out.append(text)
    out.extend(old[idx:])
    return "\n".join(out)


CODING = re.compile(r"^[ \t\f]*#.*?coding[:=]")


def _tokens(text: str):
    import io
    import tokenize

    skip = (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING)
    return [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(text).readline)
            if t.type not in skip]


def same_code(old: str, new: str) -> bool:
    """True only if Python sees identical code: same tokens apart from comments,
    blank lines and spacing. Encoding declarations change how a file is read, so a
    change touching one in the first two lines never counts as 'same'."""
    head = lambda s: [l for l in s.split("\n")[:2] if CODING.match(l)]
    if head(old) != head(new):
        return False
    try:
        return _tokens(old) == _tokens(new)
    except Exception:
        return False


class Selector:
    def __init__(self, tests, mp):
        self.all = {t["number"] for t in tests}
        self.sets = [set(s) for s in mp["sets"]]
        self.files = mp["files"]
        self.opened = mp["opened"]
        self.stmt_cache: dict = {}

    def whole_file(self, path):
        info = self.files.get(path)
        return self.sets[info["all"]] if info else set()

    def select_py(self, ch: FileChange, path: str, why: list) -> set:
        info = self.files.get(path)
        src_file = SRC / path
        if not src_file.exists():
            why.append(f"{path}: no pristine copy -> ALL")
            return self.all
        original = src_file.read_text()
        new_text = rebuild(original, ch.hunks)
        if new_text is None:
            why.append(f"{path}: patch does not apply -> ALL")
            return self.all
        try:
            compile(new_text, path, "exec")
        except SyntaxError:
            why.append(f"{path}: does not compile -> whole file")
            return self.whole_file(path) if info else self.all
        if same_code(original, new_text):
            why.append(f"{path}: only comments/blank lines/spacing changed -> none")
            return set()
        if info is None:
            why.append(f"{path}: never run by any test -> none")
            return set()
        chosen = set()
        funcs = info["funcs"]
        stmts = self.stmt_cache.get(path)
        if stmts is None:
            stmts = ({l: s for l, s in info.get("stmts", [])}, {l: f for l, f in info.get("multi", [])})
            self.stmt_cache[path] = stmts
        stmt_set, multi = stmts
        kinds = []
        for p in ch.points:
            best = None
            for start, body, end, s in funcs:
                if body <= p <= end and (best is None or end - body < best[1] - best[0]):
                    best = (body, end, s)
            if best is None:
                # module / class level, decorators, signatures: run at import time
                chosen |= self.whole_file(path)
                kinds.append("file")
            elif multi.get(p, p) in stmt_set:
                # an executable statement: only tests that actually ran it can notice
                chosen |= self.sets[stmt_set[multi.get(p, p)]]
                kinds.append("stmt")
            else:
                # blank/comment/else:/docstring line inside a function: whole function
                chosen |= self.sets[best[2]]
                kinds.append("func")
        why.append(f"{path}: points stmt {kinds.count('stmt')}, func {kinds.count('func')}, "
                   f"file {kinds.count('file')}")
        return chosen

    def select_other(self, path: str, why: list) -> set:
        if path in self.opened:
            why.append(f"{path}: opened by tests")
            return self.sets[self.opened[path]]
        parent = str(Path(path).parent)
        if parent in self.opened:
            why.append(f"{path}: folder listed by tests")
            return self.sets[self.opened[parent]]
        if path.startswith(("sqlglot/", "tests/")):
            why.append(f"{path}: unknown non-Python file -> ALL")
            return self.all
        why.append(f"{path}: outside sqlglot/ and tests/, never read -> none")
        return set()

    def select(self, patch_text: str):
        why: list[str] = []
        try:
            changes = parse_patch(patch_text)
        except Exception as e:  # never crash on a strange patch
            return self.all, [f"parse error {e!r} -> ALL"]
        if not changes:
            return self.all, ["no file changes parsed -> ALL"]
        chosen: set = set()
        for ch in changes:
            if ch.binary:
                chosen |= self.all
                why.append("binary change -> ALL")
            elif ch.old is None or ch.new is None or ch.old != ch.new:
                path = ch.new or ch.old
                if path.endswith(".py") or path.startswith(("sqlglot/", "tests/")):
                    why.append(f"{ch.old} -> {ch.new}: add/delete/rename -> ALL")
                    chosen |= self.all
                else:
                    chosen |= self.select_other(path, why)
            elif ch.old.endswith(".py"):
                chosen |= self.select_py(ch, ch.old, why)
            else:
                chosen |= self.select_other(ch.old, why)
        return chosen, why


def split(numbers, durations):
    """Try 1..8 workers (longest test first onto the lightest worker); keep the cheapest."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: -durations[n])
    best = None
    for k in range(1, 9):
        workers = [[] for _ in range(k)]
        loads = [0] * k
        for n in order:
            i = loads.index(min(loads))
            workers[i].append(n)
            loads[i] += durations[n]
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, [w for w in workers if w])
    return best[1]


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    durations = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_numbers = set(durations)
    try:
        with gzip.open(MAP, "rt") as f:
            sel = Selector(tests, json.load(f))
    except Exception as e:
        log(f"map failed to load ({e!r}); running everything")
        sel = None
    for patch in sorted((DATA / "commits").glob("*.patch")):
        if sel is None:
            chosen, why = all_numbers, ["no map -> ALL"]
        else:
            try:
                chosen, why = sel.select(patch.read_text(errors="replace"))
            except Exception as e:
                chosen, why = all_numbers, [f"selector error {e!r} -> ALL"]
        chosen = {n for n in chosen if n in durations}
        workers = split(chosen, durations)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        log(f"{patch.stem}: {len(chosen)} tests, {len(workers)} workers | " + "; ".join(why))


if __name__ == "__main__":
    main()
