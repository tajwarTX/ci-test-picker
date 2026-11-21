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

import ast
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

START = time.monotonic()

DATA = Path("/data")
APP = Path(__file__).resolve().parent
MAP = APP / "map.json.gz"
MAP_PICKLE = APP / "map.pickle"  # same content, written at build time (faster to load)
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
            # A "block" is a run of -/+ lines between context lines. A block that only
            # replaces lines one for one touches just the removed lines. A block that
            # adds more lines than it removes inserts new code, which may start a new
            # statement next to its neighbours, so those neighbours count too.
            minus = plus = 0
            block_start = pos

            def close_block():
                if plus > minus:
                    cur.points.add(pos - 1)
                    cur.points.add(pos)
                    if minus:
                        cur.points.add(block_start - 1)

            # Read exactly as many lines as the header promises.
            while i < len(lines) and (old_left > 0 or new_left > 0):
                l = lines[i]
                i += 1
                if l.startswith("\\"):  # "\ No newline at end of file"
                    continue
                tag = l[:1] or " "
                body.append(l if l else " ")
                if tag == "-":
                    if not minus and not plus:
                        block_start = pos
                    cur.points.add(pos)
                    pos += 1
                    old_left -= 1
                    minus += 1
                elif tag == "+":
                    if not minus and not plus:
                        block_start = pos
                    new_left -= 1
                    plus += 1
                else:
                    close_block()
                    minus = plus = 0
                    pos += 1
                    old_left -= 1
                    new_left -= 1
            close_block()
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
# what comparison_swaps' pre-filter removes before comparing an old and a new line
_CMP_STRIP = re.compile(r"\s+|==|!=|<=|>=|<|>|\bis\b|\bnot\b")
_BOP_STRIP = re.compile(r"\s+|\band\b|\bor\b")


def _tokens(text: str):
    import io
    import tokenize

    skip = (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING)
    return [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(text).readline)
            if t.type not in skip]


_CMP_OPNAME = {"==": "Eq", "!=": "NotEq", "<": "Lt", "<=": "LtE", ">": "Gt", ">=": "GtE",
               "is": "Is", "is not": "IsNot"}


def _cmp_tokens(text: str):
    """[(type, string, (row, col))] for every token except ENCODING, with `is` directly
    followed by `not` merged into one "is not" token (Python's `is not` operator)."""
    import io
    import tokenize

    out = []
    for t in tokenize.generate_tokens(io.StringIO(text).readline):
        if t.type == tokenize.ENCODING:
            continue
        if t.type == tokenize.NAME and t.string == "not" and out and out[-1][0] == tokenize.NAME \
                and out[-1][1] == "is":
            out[-1] = (tokenize.OP, "is not", out[-1][2])
            continue
        out.append((t.type, t.string, t.start))
    return out


def _positioned_children(node):
    """Child nodes that carry source positions; unpositioned ones (comprehension,
    arguments, withitem, match_case, ...) are looked through."""
    for c in ast.iter_child_nodes(node):
        if getattr(c, "end_lineno", None) is not None and getattr(c, "lineno", None) is not None:
            yield c
        else:
            yield from _positioned_children(c)


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


def literal_canon(node):
    """Same key names as the mapper uses, for keys written as literals."""
    try:
        v = ast.literal_eval(node)
    except Exception:
        return None

    def c(v):
        if v is None:
            return "none"
        if isinstance(v, str):
            return "str:" + v
        if isinstance(v, (bool, int, float)):
            return "num:" + repr(float(v) if float(v) == v else v)
        if isinstance(v, tuple):
            parts = [c(x) for x in v]
            return None if None in parts else "tuple:(" + ",".join(parts) + ")"
        return None

    return c(v)


def touches_hash(ch) -> bool:
    """Some changed line contains '#': a comment may be all that changed."""
    return any(l[:1] in "+-" and "#" in l for _, body in ch.hunks for l in body)


def maybe_comment_only(ch) -> bool:
    """Cheap pre-check before the (slow) token comparison. If the changed lines still
    differ after ignoring whitespace and lines that start with '#', the change touches
    code, so it is NOT comment-only. Can only answer 'maybe' or 'no', never a wrong 'yes'."""
    removed, added = [], []
    for _, body in ch.hunks:
        for l in body:
            if l[:1] == "-":
                removed.append(l[1:])
            elif l[:1] == "+":
                added.append(l[1:])

    def code(lines):
        return ["".join(l.split()) for l in lines if l.strip() and not l.strip().startswith("#")]

    return code(removed) == code(added)


class ImportChecker:
    """Imports every sqlglot module from a patched copy of the pristine tree in /tmp."""

    def __init__(self, src=SRC, work=Path("/tmp/ic"), module_list=APP / "import_ok.json"):
        self.src, self.work, self.module_list = src, work, module_list
        self.ready = False

    def _setup(self):
        if self.work.exists():
            shutil.rmtree(self.work)
        self.work.mkdir(parents=True)
        # copy2 keeps modification times, so the build-time bytecode stays valid
        shutil.copytree(self.src / "sqlglot", self.work / "sqlglot", copy_function=shutil.copy2)
        self.ready = True

    def __call__(self, patched: dict) -> bool:
        """patched: {"sqlglot/x.py": new text}. True only if every module imports."""
        if not self.module_list.exists():
            return False
        if not self.ready:
            self._setup()
        saved = {}
        try:
            for rel, text in patched.items():
                f = self.work / rel
                st = f.stat()
                saved[rel] = (f.read_bytes(), st.st_atime_ns, st.st_mtime_ns)
                f.write_text(text)
            env = dict(os.environ, PYTHONPATH=str(self.work), PYTHONDONTWRITEBYTECODE="1")
            mods = [rel[:-3].replace("/", ".").removesuffix(".__init__") for rel in patched]
            proc = subprocess.run(
                [sys.executable, str(APP / "importcheck.py"), str(self.module_list), *mods],
                cwd="/tmp", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60,
            )
            return proc.returncode == 0
        except Exception:
            return False
        finally:
            for rel, (data, atime, mtime) in saved.items():
                f = self.work / rel
                f.write_bytes(data)
                os.utime(f, ns=(atime, mtime))


class TpcProbe:
    """Runs /app/tpc_probe.py on the patched sqlglot copy that ImportChecker keeps in
    /tmp: re-runs only the TPC-DS queries whose code paths execute the changed
    statements and compares them with pristine results recorded at build time."""

    BUDGET = 12.0  # most CPU seconds a probe may be expected to cost
    RUN_LIMIT = 90.0  # no new probe once the whole run has taken this long (limit: 180-270 s)
    OVERHEAD = 1.5  # start-up: interpreter, sqlglot import, table data

    def __init__(self, checker, map_path=APP / "tpc_map.json.gz", data=APP / "tpc"):
        self.checker, self.map_path, self.data = checker, map_path, data
        self.loaded = None

    def _load(self):
        if self.loaded is None:
            try:
                m = json.load(gzip.open(self.map_path, "rt"))
                pr = json.load(open(self.data / "pristine.json"))["queries"]
                m["base"] = {r: set(v) for r, v in m["base"].items()}
                m["q"] = {r: dict((l, b) for l, b in v) for r, v in m["q"].items()}
                m["unmodeled"] = {r: set(v) for r, v in m["unmodeled"].items()}
                self.loaded = (m, pr)
            except Exception:
                self.loaded = False
        return self.loaded

    def affected(self, ranges):
        """Query indices whose paths run a line in any (rel, lo, hi); None = no probe."""
        got = self._load()
        if not got:
            return None
        m, pr = got
        mask = 0
        for rel, lo, hi in ranges:
            base, qs, unm = m["base"].get(rel, ()), m["q"].get(rel, {}), m["unmodeled"].get(rel, ())
            for l in range(lo, hi + 1):
                if l in base or l in unm:
                    return None
                mask |= qs.get(l, 0)
        out = [i for i in range(len(pr)) if mask >> i & 1]
        if any(pr[i].get("unstable") for i in out):
            return None
        return out

    def run_max(self, patched):
        return _run_patched(self.checker, patched, ["maxnodes"], timeout=30)

    def cost(self, queries):
        return self.OVERHEAD + sum(self.loaded[1][i]["cpu"] for i in queries)

    def run(self, patched, queries, timeout):
        if not queries:
            return True
        c = self.checker
        if not c.ready:
            c._setup()
        saved = {}
        try:
            for rel, text in patched.items():
                f = c.work / rel
                st = f.stat()
                saved[rel] = (f.read_bytes(), st.st_atime_ns, st.st_mtime_ns)
                f.write_text(text)
            env = dict(os.environ, PYTHONPATH=str(c.work), PYTHONDONTWRITEBYTECODE="1",
                       PYTHONHASHSEED="0", TPC_SRC=str(SRC))
            order = sorted(queries, key=lambda i: self.loaded[1][i]["cpu"])
            proc = subprocess.run(
                [sys.executable, str(APP / "tpc_probe.py"), "probe", str(self.data), *map(str, order)],
                cwd="/tmp", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            return proc.returncode == 0
        except Exception:
            return False
        finally:
            for rel, (data, atime, mtime) in saved.items():
                f = c.work / rel
                f.write_bytes(data)
                os.utime(f, ns=(atime, mtime))


def _run_patched(checker, patched, args, timeout):
    """Run /app/tpc_probe.py ARGS with the patched sqlglot copy in /tmp on sys.path."""
    if not checker.ready:
        checker._setup()
    saved = {}
    try:
        for rel, text in patched.items():
            f = checker.work / rel
            st = f.stat()
            saved[rel] = (f.read_bytes(), st.st_atime_ns, st.st_mtime_ns)
            f.write_text(text)
        env = dict(os.environ, PYTHONPATH=str(checker.work), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONHASHSEED="0", TPC_SRC=str(SRC))
        proc = subprocess.run([sys.executable, str(APP / "tpc_probe.py"), *args], cwd="/tmp", env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
        return proc.returncode == 0
    except Exception:
        return False
    finally:
        for rel, (data, atime, mtime) in saved.items():
            f = checker.work / rel
            f.write_bytes(data)
            os.utime(f, ns=(atime, mtime))


def _blocks(s):
    for name in ("body", "orelse", "finalbody"):
        b = getattr(s, name, None)
        if b:
            yield b
    for h in getattr(s, "handlers", None) or []:
        yield h.body
    for c in getattr(s, "cases", None) or []:
        yield c.body


_ENCLOSING = "enclosing function"


def _no_code(s):
    return isinstance(s, (ast.Pass, ast.Global, ast.Nonlocal)) \
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) \
        or (isinstance(s, ast.AnnAssign) and s.value is None)


def _func_ranges(tree, points):
    """For each point: the line range of the innermost statement holding it inside a
    function body (code that runs only when the function is called); a point between
    statements gets the enclosing statement's (or the whole function's) range. None if
    any point is outside function bodies (module/class level, def lines, decorators,
    signatures: all of that runs at import)."""

    def find(stmts, p, in_func):
        for s in stmts:
            lo, hi = s.lineno, s.end_lineno or s.lineno
            if not lo <= p <= hi:
                continue
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if s.body and s.body[0].lineno <= p:
                    r = find(s.body, p, True)
                    return r if r not in (None, _ENCLOSING) else (s.body[0].lineno, hi)
                return None
            if isinstance(s, ast.ClassDef):
                return find(s.body, p, in_func)
            if not in_func:
                return None
            if _no_code(s):
                # a docstring, pass, global/nonlocal or bare annotation has no line of
                # its own that runs: it matters whenever the enclosing function runs
                return _ENCLOSING
            for block in _blocks(s):
                r = find(block, p, True)
                if r is not None:
                    return r
            return (lo, hi)
        return None

    out = []
    for p in points:
        r = find(tree.body, p, False)
        if r is None:
            return None
        out.append(r)
    return out


def cost_of(workers, durations):
    """Simulated seconds of a split: busiest worker + 2 s per worker."""
    if not workers:
        return 0.0
    return (max(sum(durations[n] for n in w) for w in workers) + 2000 * len(workers)) / 1000


PYTEST_FILES = {"conftest.py", "pytest.ini", "tox.ini"}
PYTEST_SECTION_FILES = {"pyproject.toml", "setup.cfg"}


def touches_pytest_config(ch, path) -> bool:
    """pytest reads conftest.py, pytest.ini, tox.ini and the [tool.pytest...] /
    [tool:pytest] sections of pyproject.toml / setup.cfg before any test runs, so a change
    there can affect every test."""
    name = path.rsplit("/", 1)[-1]
    if name in PYTEST_FILES:
        return True
    if name not in PYTEST_SECTION_FILES:
        return False
    for _, body in ch.hunks:
        if any("pytest" in l.lower() for l in body):
            return True
    original = SRC / path
    if not original.exists():
        return True  # cannot tell which section changed
    section = ""
    sections = {}
    for i, line in enumerate(original.read_text(errors="replace").split("\n"), 1):
        if line.strip().startswith("["):
            section = line.strip().lower()
        sections[i] = section
    return any("pytest" in sections.get(p, "") for p in ch.points)


class LazySets:
    """Turns a stored test list into a set only when a commit needs it."""

    def __init__(self, lists):
        self.lists = lists
        self.cache: dict = {}

    def __getitem__(self, i):
        s = self.cache.get(i)
        if s is None:
            s = self.cache[i] = set(self.lists[i])
        return s


class Selector:
    def __init__(self, tests, mp, import_check=None, tpc_probe=None):
        self.all = {t["number"] for t in tests}
        self.durations = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.sets = LazySets(mp["sets"])
        self.files = mp["files"]
        self.opened = mp["opened"]
        self.stmt_cache: dict = {}
        self.import_check = import_check or ImportChecker()
        self.containers = mp.get("containers", {})
        self.container_index: dict = {}
        for cid, c in self.containers.items():
            self.container_index.setdefault(c["file"], []).append((c["start"], c["end"], cid))
        self.ast_cache: dict = {}
        self.dump_cache: dict = {}
        self.tok_cache: dict = {}
        tpc = [t["number"] for t in tests if t["id"] == "tests/test_executor.py::TestExecutor::test_execute_tpcds"]
        if tpc_probe is None and isinstance(self.import_check, ImportChecker):
            tpc_probe = TpcProbe(self.import_check)
        self.tpc_probe = tpc_probe
        self.tpc = tpc[0] if tpc and tpc_probe is not None else None
        mx = [t["number"] for t in tests if t["id"] == "tests/test_parser.py::TestParser::test_max_nodes"]
        self.max_test = mx[0] if mx and tpc_probe is not None else None
        self._max_map = None
        self.cmp_differ = mp.get("cmp_differ", {})  # site -> {partner op: tests}
        self.cmp_files = set(mp.get("cmp_files", ()))  # files whose comparisons were profiled
        # site -> tests where and/or swapped could differ. Only maps from the side-effect-aware
        # profiler (bop_version >= 2) are used: version 1 ignored effects of the skipped operand.
        self.bop = mp.get("bop_infected", {}) if mp.get("bop_version", 1) >= 2 else {}
        self.bop_tests = set(mp.get("bop_tests", ())) if self.bop or mp.get("bop_version", 1) >= 2 else set()
        # per commit
        self.patched: dict = {}
        self.fallback: set = set()

    def container_at(self, path, p):
        """Innermost tracked container whose braces strictly enclose line p."""
        best = None
        for start, end, cid in self.container_index.get(path, ()):
            if start < p < end and (best is None or end - start < best[1] - best[0]):
                best = (start, end, cid)
        return best[2] if best else None

    def container_edge(self, path, p, original, new_text):
        """The innermost tracked container holding line p, provided the WHOLE edit of
        this file is confined strictly inside its braces: every token before its `{`
        and from its matching `}` on is unchanged, and in the new file that `{` still
        opens a container closed by that `}`. Otherwise None (class/setting rules)."""
        best = None
        for start, end, cid in self.container_index.get(path, ()):
            if start <= p <= end and (best is None or end - start < best[1] - best[0]):
                best = (start, end, cid)
        if best is None or "\r" in original or "\r" in new_text:
            return None
        cid = best[2]
        if self.containers[cid]["kind"] not in ("dict", "set"):
            return None
        _, line, col = cid.rsplit(":", 2)
        line, col = int(line), int(col)
        try:
            old_tree = self.ast_cache.get(path)
            if old_tree is None:
                old_tree = self.ast_cache[path] = ast.parse(original)
            new_tree = ast.parse(new_text)
            old_toks = self.tok_cache.get(path)
            if old_toks is None:
                old_toks = self.tok_cache[path] = _cmp_tokens(original)
            new_toks = _cmp_tokens(new_text)
        except Exception:
            return None
        want = (ast.Dict, ast.Set)
        node = next((n for n in ast.walk(old_tree) if isinstance(n, want)
                     and n.lineno == line and n.col_offset == col), None)
        if node is None:
            return None
        lines = original.split("\n")

        def char_col(row, bcol):
            return len(lines[row - 1].encode("utf-8")[:bcol].decode("utf-8", "replace"))

        open_pos = (node.lineno, char_col(node.lineno, node.col_offset))
        close_pos = (node.end_lineno, char_col(node.end_lineno, node.end_col_offset) - 1)
        oi = next((k for k, t in enumerate(old_toks) if t[2] == open_pos and t[1] == "{"), None)
        ci = next((k for k, t in enumerate(old_toks) if t[2] == close_pos and t[1] == "}"), None)
        if oi is None or ci is None:
            return None
        n_old, n_new = len(old_toks), len(new_toks)
        i = 0
        while i < min(n_old, n_new) and old_toks[i][:2] == new_toks[i][:2]:
            i += 1
        if i == n_old == n_new:
            return None
        j = 0
        while j < min(n_old, n_new) - i and old_toks[n_old - 1 - j][:2] == new_toks[n_new - 1 - j][:2]:
            j += 1
        if not (oi < i and ci >= n_old - j):
            return None  # the edit reaches outside the braces
        # in the new file the same `{` must open a container closed by the same `}`
        nci = ci - n_old + n_new
        nopen, nclose = new_toks[oi][2], new_toks[nci][2]
        new_lines = new_text.split("\n")
        for n in ast.walk(new_tree):
            if isinstance(n, want) and n.lineno == nopen[0] and \
                    len(new_lines[n.lineno - 1].encode("utf-8")[:n.col_offset].decode("utf-8", "replace")) == nopen[1]:
                end_c = len(new_lines[n.end_lineno - 1].encode("utf-8")[:n.end_col_offset].decode("utf-8", "replace")) - 1
                return cid if (n.end_lineno, end_c) == nclose else None
        return None

    def container_change(self, path, original, new_text, cid, ch):
        """Tests affected by the entry changes of one container, or None if unsure."""
        info = self.containers[cid]
        if info["kind"] not in ("dict", "set"):
            return None
        _, line, col = cid.rsplit(":", 2)
        line, col = int(line), int(col)
        try:
            old_tree = self.ast_cache.get(path)
            if old_tree is None:
                old_tree = self.ast_cache[path] = ast.parse(original)
            new_tree = ast.parse(new_text)
        except SyntaxError:
            return None
        want = ast.Dict if info["kind"] == "dict" else ast.Set
        delta = 0  # lines added minus removed before the container's first line
        for start, body in ch.hunks:
            old_ln = start
            for l in body:
                tag = l[:1]
                if tag == " ":
                    old_ln += 1
                elif tag == "-":
                    if old_ln < line:
                        delta -= 1
                    old_ln += 1
                elif tag == "+":
                    if old_ln <= line:
                        delta += 1
        find = lambda tree, ln: next((n for n in ast.walk(tree) if isinstance(n, want)
                                      and n.lineno == ln and n.col_offset == col), None)
        old_node, new_node = find(old_tree, line), find(new_tree, line + delta)
        if old_node is None or new_node is None:
            return None

        def items(node, text):
            seg = lambda n: ast.get_source_segment(text, n)
            out = []
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    out.append(("u", seg(v), None) if k is None else ("k", seg(k), seg(v), k))
            else:
                for e in node.elts:
                    out.append(("u", seg(e.value), None) if isinstance(e, ast.Starred) else ("k", seg(e), "", e))
            return out

        old_items, new_items = items(old_node, original), items(new_node, new_text)
        if [i[1] for i in old_items if i[0] == "u"] != [i[1] for i in new_items if i[0] == "u"]:
            return None  # what it is built from changed
        old_explicit = [i for i in old_items if i[0] == "k"]
        if len(old_explicit) != len(info["entries"]):
            return None
        key_canon = {}
        old_pairs = []
        for item, entry in zip(old_explicit, info["entries"]):
            key_canon.setdefault(item[1], set()).add(entry[2])
            old_pairs.append((entry[2], item[2]))
        new_pairs = []
        for item in (i for i in new_items if i[0] == "k"):
            c = literal_canon(item[3])
            if c is None:
                known = key_canon.get(item[1])
                c = next(iter(known)) if known and len(known) == 1 else None
            new_pairs.append((c, item[2]))
        from collections import Counter

        diff = (Counter(old_pairs) - Counter(new_pairs)) + (Counter(new_pairs) - Counter(old_pairs))
        changed = {k for k, _ in diff}
        if None in changed:
            return None
        out = set(self.sets[info["whole"]])
        for k in changed:
            if k in info["acc"]:
                out |= self.sets[info["acc"][k]]
        return out

    def same_tree(self, path, original, new_text) -> bool:
        head = lambda t: [l for l in t.split("\n")[:2] if CODING.match(l)]
        if head(original) != head(new_text):
            return False
        try:
            old = self.dump_cache.get(path)
            if old is None:
                old = self.dump_cache[path] = ast.dump(ast.parse(original), include_attributes=False)
            return old == ast.dump(ast.parse(new_text), include_attributes=False)
        except Exception:
            return False

    CMP_PARTNERS = {
        "Is": ("Eq",), "Eq": ("Is",), "IsNot": ("NotEq",), "NotEq": ("IsNot",),
        "Lt": ("LtE", "Gt", "GtE"), "LtE": ("Lt", "Gt", "GtE"),
        "Gt": ("GtE", "Lt", "LtE"), "GtE": ("Gt", "Lt", "LtE"),
    }

    def comparison_swaps(self, path, original, new_text, ch):
        """[(site, new op)] if the ONLY difference is swapped operators in single-operator
        comparisons that were profiled; otherwise None."""
        if path not in self.cmp_files:
            return None
        # Cheap text pre-filter (it can only send the change to the normal rules): every
        # hunk must replace lines one-for-one, and each pair must be identical once
        # comparison operators, `is`/`not` and spacing are removed.
        found_op = False
        for _, body in ch.hunks:
            minus = [l[1:] for l in body if l[:1] == "-"]
            plus = [l[1:] for l in body if l[:1] == "+"]
            if len(minus) != len(plus):
                return None
            for a, b in zip(minus, plus):
                if a == b:
                    continue
                if _CMP_STRIP.sub("", a) != _CMP_STRIP.sub("", b):
                    return None
                found_op = True
        if not found_op:
            return None
        if "\r" in original or "\r" in new_text:
            return None
        # Proof, step 1: the token streams are identical except for comparison-operator
        # tokens replaced by a partner. All comparison operators sit at the same place in
        # Python's grammar, so the two programs then parse to the same tree except for
        # those operators.
        try:
            old_toks = self.tok_cache.get(path)
            if old_toks is None:
                old_toks = self.tok_cache[path] = _cmp_tokens(original)
            new_toks = _cmp_tokens(new_text)
            old_tree = self.ast_cache.get(path)
            if old_tree is None:
                old_tree = self.ast_cache[path] = ast.parse(original)
        except Exception:
            return None
        if len(old_toks) != len(new_toks):
            return None
        changed = []
        for (ka, sa, pa), (kb, sb, _) in zip(old_toks, new_toks):
            if sa == sb and ka == kb:
                continue
            oa, ob = _CMP_OPNAME.get(sa), _CMP_OPNAME.get(sb)
            if oa is None or ob is None or ob not in self.CMP_PARTNERS[oa]:
                return None
            changed.append((pa, oa, ob))
        if not changed:
            return None
        # Step 2: each changed token must be THE operator of a single-operator Compare
        # in the original file (the kind the profiler rewrote): find it by walking down
        # the tree to the innermost node whose span holds the token.
        lines = original.split("\n")
        swaps, seen = [], set()
        for (row, col), oa, ob in changed:
            pos = (row, len(lines[row - 1][:col].encode("utf-8")))  # ast columns are bytes
            node = old_tree
            while True:
                for c in _positioned_children(node):
                    if (c.lineno, c.col_offset) <= pos < (c.end_lineno, c.end_col_offset):
                        node = c
                        break
                else:
                    break
            if not isinstance(node, ast.Compare) or len(node.ops) != 1 or id(node) in seen:
                return None
            left, right = node.left, node.comparators[0]
            if not ((left.end_lineno, left.end_col_offset) <= pos < (right.lineno, right.col_offset)):
                return None
            if type(node.ops[0]).__name__ != oa:
                return None
            seen.add(id(node))
            swaps.append((f"{path}:{node.lineno}:{node.col_offset}", ob))
        return swaps

    def boolop_swaps(self, path, original, new_text, ch):
        """[site] if the ONLY difference is `and` <-> `or` in two-operand BoolOps that were
        profiled; otherwise None."""
        if not self.bop_tests or path not in self.cmp_files:
            return None
        found = False
        for _, body in ch.hunks:
            minus = [l[1:] for l in body if l[:1] == "-"]
            plus = [l[1:] for l in body if l[:1] == "+"]
            if len(minus) != len(plus):
                return None
            for a, b in zip(minus, plus):
                if a == b:
                    continue
                if _BOP_STRIP.sub("", a) != _BOP_STRIP.sub("", b):
                    return None
                found = True
        if not found or "\r" in original or "\r" in new_text:
            return None
        try:
            old_toks = self.tok_cache.get(path)
            if old_toks is None:
                old_toks = self.tok_cache[path] = _cmp_tokens(original)
            new_toks = _cmp_tokens(new_text)
            old_tree = self.ast_cache.get(path)
            if old_tree is None:
                old_tree = self.ast_cache[path] = ast.parse(original)
            new_tree = ast.parse(new_text)
        except Exception:
            return None
        if len(old_toks) != len(new_toks):
            return None
        changed = []
        for (ka, sa, pa), (kb, sb, pb) in zip(old_toks, new_toks):
            if sa == sb and ka == kb:
                continue
            if {sa, sb} != {"and", "or"}:
                return None
            changed.append((pa, pb, sa))
        if not changed:
            return None
        old_lines, new_lines = original.split("\n"), new_text.split("\n")

        def boolop_at(tree, lines, rc, word):
            row, col = rc
            pos = (row, len(lines[row - 1][:col].encode("utf-8")))
            node = tree
            while True:
                for c in _positioned_children(node):
                    if (c.lineno, c.col_offset) <= pos < (c.end_lineno, c.end_col_offset):
                        node = c
                        break
                else:
                    break
            if not isinstance(node, ast.BoolOp) or len(node.values) != 2:
                return None
            a, b = node.values
            if not ((a.end_lineno, a.end_col_offset) <= pos < (b.lineno, b.col_offset)):
                return None
            if type(node.op).__name__ != {"and": "And", "or": "Or"}[word]:
                return None
            return node

        sites, seen = [], set()
        for pa, pb, word in changed:
            old_node = boolop_at(old_tree, old_lines, pa, word)
            new_node = boolop_at(new_tree, new_lines, pb, "or" if word == "and" else "and")
            if old_node is None or new_node is None or id(old_node) in seen:
                return None
            seen.add(id(old_node))
            new_node.op = old_node.op  # undo the swap in the new tree, then compare trees
            sites.append(f"{path}:{old_node.lineno}:{old_node.col_offset}")
        same = self.dump_cache.get(path)
        if same is None:
            same = self.dump_cache[path] = ast.dump(old_tree, include_attributes=False)
        if same != ast.dump(new_tree, include_attributes=False):
            return None
        return sites

    SOURCE_READ = ("sqlglot/typing/",)  # tests/test_typing.py reads these files' source text

    def no_runtime_effect(self, path, original, new_text):
        """"docstring" / "overload" if the ONLY differences are docstring text (each
        docstring still non-empty, or still empty) or code inside @overload stubs; else None."""
        if not path.startswith("sqlglot/") or path.startswith(self.SOURCE_READ):
            return None
        try:
            a, b = ast.parse(original), ast.parse(new_text)
        except SyntaxError:
            return None

        def is_overload(fn):
            for d in fn.decorator_list:
                if (isinstance(d, ast.Name) and d.id == "overload") or \
                        (isinstance(d, ast.Attribute) and d.attr == "overload" and isinstance(d.value, ast.Name)
                         and d.value.id in ("t", "typing")):
                    return True
            return False

        def normalize(tree):
            docs, stubs = [], []
            for n in ast.walk(tree):
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
                    d = n.body[0]
                    if isinstance(d, ast.Expr) and isinstance(d.value, ast.Constant) and isinstance(d.value.value, str):
                        docs.append(d.value.value)
                        d.value.value = ""
            for n in ast.walk(tree):
                for field in ("body", "orelse", "finalbody"):
                    seq = getattr(n, field, None)
                    if isinstance(seq, list):
                        for i, st in enumerate(seq):
                            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and is_overload(st):
                                stubs.append(ast.dump(st))
                                seq[i] = ast.Pass()
            return docs, stubs, ast.dump(tree)

        da, sa, ta = normalize(a)
        db, sb, tb = normalize(b)
        if ta != tb or len(da) != len(db) or any(bool(x) != bool(y) for x, y in zip(da, db)):
            return None
        if sa != sb:
            return "overload"
        return "docstring" if da != db else None

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
        if maybe_comment_only(ch) and same_code(original, new_text):
            why.append(f"{path}: only comments/blank lines/spacing changed -> none")
            return set()
        if touches_hash(ch) and self.same_tree(path, original, new_text):
            # e.g. a comment edited at the end of a code line: Python builds exactly the
            # same program (only line numbers can differ)
            why.append(f"{path}: same syntax tree (comment/format edit) -> none")
            return set()
        kind = self.no_runtime_effect(path, original, new_text)
        if kind == "docstring" or (kind == "overload" and self.import_check({path: new_text})):
            # Only docstring text (still non-empty) or @overload stubs changed: no scored
            # test reads docstrings, line numbers or overload stubs, and sqlglot's one
            # docstring reader (Expr.__init_subclass__) only checks emptiness. Files whose
            # source a test reads are excluded; stub changes must still import.
            why.append(f"{path}: {kind} change only (no runtime effect) -> none")
            return set()
        if info is None:
            why.append(f"{path}: never run by any test -> none")
            return set()
        swaps = self.comparison_swaps(path, original, new_text, ch)
        if swaps is not None:
            # Only comparison operators were swapped (e.g. `is` -> `==`). A test is
            # affected only if, at one of these comparisons, the two operators gave
            # different answers on the values that test actually compared.
            out = set()
            for site, new_op in swaps:
                sid_ = self.cmp_differ.get(site, {}).get(new_op)
                if sid_ is not None:
                    out |= self.sets[sid_]
            why.append(f"{path}: comparison swap at {len(swaps)} site(s) -> tests where they disagree")
            return out
        bsites = self.boolop_swaps(path, original, new_text, ch)
        chosen = set()
        funcs = info["funcs"]
        stmts = self.stmt_cache.get(path)
        if stmts is None:
            stmts = ({l: s for l, s in info.get("stmts", [])}, {l: f for l, f in info.get("multi", [])})
            self.stmt_cache[path] = stmts
        stmt_set, multi = stmts
        if path.startswith("sqlglot/"):
            self.patched[path] = new_text
        kinds = []

        def import_time(p):
            """A point outside any function body (module level, class body, decorators,
            signatures): all of it runs when the module is imported."""
            out = set()
            # Outside any function body: module level, class body, decorators,
            # signatures. All of it runs at import time.
            cls = None
            if path.startswith("sqlglot/"):
                for start, body, end, s in info.get("classes", []):
                    if body <= p <= end and (cls is None or end - body < cls[1] - cls[0]):
                        cls = (body, end, s)
            setting = None
            for start, end, s in info.get("settings", []):
                if start <= p <= end:
                    setting = s
            if cls is not None and cls[2] >= 0 and setting is not None:
                # An UPPER_CASE class setting: tests that used this class (or a
                # subclass) AND ran a line that reads the setting by name.
                out |= self.sets[cls[2]] & self.sets[setting]
                self.fallback |= self.whole_file(path)
                kinds.append("setting")
            elif cls is not None and cls[2] >= 0:
                # Class body / method header: tests that used this class (or a
                # subclass), plus tests that ran a method whose header this is.
                out |= self.sets[cls[2]]
                for start, body, end, s in funcs:
                    if start <= p < body:
                        out |= self.sets[s]
                # ...but only if the patched package still imports (checked once
                # per commit); otherwise everything that loads this file.
                self.fallback |= self.whole_file(path)
                kinds.append("class")
            else:
                msetting = None
                if cls is None and path.startswith("sqlglot/"):
                    for start, end, s in info.get("msettings", []):
                        if start <= p <= end:
                            msetting = s
                if msetting is not None:
                    # An UPPER_CASE module constant: tests that loaded this file AND
                    # ran a line that reads the constant by name (import-checked).
                    out |= self.whole_file(path) & self.sets[msetting]
                    self.fallback |= self.whole_file(path)
                    kinds.append("setting")
                else:
                    out |= self.whole_file(path)
                    kinds.append("file")
            return out

        container_points: dict = {}
        for p in ch.points:
            best = None
            for start, body, end, s in funcs:
                if body <= p <= end and (best is None or end - body < best[1] - best[0]):
                    best = (body, end, s)
            if best is None:
                cid = self.container_edge(path, p, original, new_text)
                if cid is not None:
                    container_points.setdefault(cid, []).append(p)
                else:
                    chosen |= import_time(p)
            elif multi.get(p, p) in stmt_set:
                # an executable statement: only tests that actually ran it can notice
                chosen |= self.sets[stmt_set[multi.get(p, p)]]
                kinds.append("stmt")
            else:
                # blank/comment/else:/docstring line inside a function: whole function
                chosen |= self.sets[best[2]]
                kinds.append("func")
        for cid, pts in container_points.items():
            base = set()
            for p in pts:
                base |= import_time(p)  # class / setting / file rule (always sound)
            got = self.container_change(path, original, new_text, cid, ch)
            if got is not None:
                # Entries of a settings dict/set: tests that looked up a changed key (in
                # it or any copy of it) or used it wholesale. Both this and the class rule
                # contain every affected test, so their intersection does too.
                chosen |= got & base
                kinds.append("entry")
            else:
                chosen |= base
        why.append(f"{path}: points stmt {kinds.count('stmt')}, func {kinds.count('func')}, "
                   f"class {kinds.count('class')}, setting {kinds.count('setting')}, entry {kinds.count('entry')}, "
                   f"file {kinds.count('file')}")
        if bsites is not None:
            # Only `and` <-> `or` swapped. The profiled (heavy) tests are affected only
            # where the swapped operator could give another value or skip an operand
            # that has effects (see the mapper's bopmap); every other test keeps the
            # rules above.
            infected = set()
            for site in bsites:
                sid_ = self.bop.get(site)
                if sid_ is not None:
                    infected |= self.sets[sid_]
            drop = (self.bop_tests - infected) & chosen
            if drop:
                chosen -= drop
                why.append(f"{path}: and/or swap: {len(drop)} profiled test(s) cannot differ -> dropped")
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
        self.patched, self.fallback = {}, set()
        for ch in changes:
            if ch.binary:
                chosen |= self.all
                why.append("binary change -> ALL")
            elif ch.old is None or ch.new is None or ch.old != ch.new:
                path = ch.new or ch.old
                names = [p for p in (ch.old, ch.new) if p]
                if any(p.endswith(".py") or p.startswith(("sqlglot/", "tests/"))
                       or p.rsplit("/", 1)[-1] in PYTEST_FILES | PYTEST_SECTION_FILES for p in names):
                    why.append(f"{ch.old} -> {ch.new}: add/delete/rename -> ALL")
                    chosen |= self.all
                else:
                    chosen |= self.select_other(path, why)
            elif touches_pytest_config(ch, ch.old):
                why.append(f"{ch.old}: pytest configuration -> ALL")
                chosen |= self.all
            elif ch.old.endswith(".py") and ch.old.startswith(("sqlglot/", "tests/")):
                chosen |= self.select_py(ch, ch.old, why)
            elif ch.old.endswith(".py") and "/" not in ch.old:
                # the repository root is on sys.path: a root module can shadow others
                why.append(f"{ch.old}: Python file at the repository root -> ALL")
                chosen |= self.all
            else:
                # other files, including Python files outside sqlglot/ and tests/ (e.g.
                # sqlglotc/setup.py): the tests recorded opening, running or listing them
                chosen |= self.select_other(ch.old, why)
        if self.fallback - chosen:
            if self.import_check(self.patched):
                why.append("class-level change: patched package imports OK")
            else:
                why.append("class-level change: import check FAILED -> whole file")
                chosen |= self.fallback
                return chosen, why  # never probe a package that does not import
        if self.tpc is not None and self.tpc in chosen:
            self.try_tpc_probe(changes, chosen, why)
        if self.max_test is not None and self.max_test in chosen:
            self.try_max_probe(changes, chosen, why)
        return chosen, why

    def try_max_probe(self, changes, chosen, why):
        """Drop test_max_nodes when the change touches no line its two huge parses (or the
        imports before them) run, and its four cheap checks pass on the patched code."""
        got = self._probe_inputs(changes, self.max_test)
        if got is None:
            return
        patched, ranges = got
        m = self.max_map()
        if not m:
            return
        for rel, lo, hi in ranges:
            huge, unm = m["huge"].get(rel, ()), m["unmodeled"].get(rel, ())
            if any(l in huge or l in unm for l in range(lo, hi + 1)):
                return
        saving = cost_of(split(chosen, self.durations), self.durations) - \
            cost_of(split(chosen - {self.max_test}, self.durations), self.durations)
        if saving < 3:
            return
        if self.tpc_probe.run_max(patched):
            chosen.discard(self.max_test)
            why.append("test_max_nodes: change outside its huge parses; cheap checks pass -> drop test_max_nodes")
        else:
            why.append("test_max_nodes: cheap checks fail -> keep")

    def max_map(self):
        if self._max_map is None:
            try:
                m = json.load(gzip.open(APP / "max_map.json.gz", "rt"))
                self._max_map = {k: {r: set(v) for r, v in m[k].items()} for k in ("huge", "unmodeled")}
            except Exception:
                self._max_map = False
        return self._max_map

    def try_tpc_probe(self, changes, chosen, why):
        """Drop test_execute_tpcds when re-running its affected queries on the patched code
        gives exactly the pristine results (see TpcProbe)."""
        got = self._probe_inputs(changes, self.tpc)
        if got is None:
            return
        patched, ranges = got
        self._tpc_probe_ranges(patched, ranges, chosen, why)

    def _probe_inputs(self, changes, test):
        """(patched files, changed line ranges inside function bodies) for a probe of one
        test, or None if the change is not of that kind. Files the test never runs or
        opens, and comment-only edits, are left out."""
        patched, ranges = {}, []
        for ch in changes:
            if ch.binary or ch.old is None or ch.old != ch.new:
                return
            path = ch.old
            if not path.endswith(".py"):
                # a data file the TPC-DS test never opened cannot change what it does
                if path.rsplit("/", 1)[-1] in PYTEST_FILES | PYTEST_SECTION_FILES:
                    return
                sid_ = self.opened.get(path)
                if sid_ is not None and test in self.sets[sid_]:
                    return
                if sid_ is None and path.startswith(("sqlglot/", "tests/")):
                    return  # never seen: be careful
                continue
            info = self.files.get(path)
            if info is None or not path.startswith(("sqlglot/", "tests/")):
                return
            if test not in self.sets[info["all"]]:
                continue  # the TPC-DS test never ran a line of this file (not even its import)
            if not path.startswith("sqlglot/"):
                return
            src = SRC / path
            if not src.exists():
                return
            original = src.read_text()
            new_text = rebuild(original, ch.hunks)
            if new_text is None:
                return
            try:
                compile(new_text, path, "exec")
                tree = self.ast_cache.get(path)
                if tree is None:
                    tree = self.ast_cache[path] = ast.parse(original)
            except Exception:
                return
            if same_code(original, new_text):
                continue  # comments/spacing only: the same program
            if not ch.points:
                return
            r = _func_ranges(tree, sorted(ch.points))
            if r is None:
                return
            ranges += [(path, lo, hi) for lo, hi in r]
            patched[path] = new_text
        if not ranges:
            return None
        return patched, ranges

    def _tpc_probe_ranges(self, patched, ranges, chosen, why):
        qs = self.tpc_probe.affected(ranges)
        if qs is None:
            return
        if time.monotonic() - START > TpcProbe.RUN_LIMIT:
            why.append("TPC-DS probe skipped (run time limit)")
            return
        cost = self.tpc_probe.cost(qs)
        saving = cost_of(split(chosen, self.durations), self.durations) - \
            cost_of(split(chosen - {self.tpc}, self.durations), self.durations)
        if cost > self.tpc_probe.BUDGET or cost > saving - 3:
            why.append(f"TPC-DS probe skipped ({len(qs)} queries, ~{cost:.1f}s CPU vs {saving:.1f}s saved)")
            return
        if self.tpc_probe.run(patched, qs, timeout=max(20.0, 4 * cost)):
            chosen.discard(self.tpc)
            why.append(f"TPC-DS probe: {len(qs)} affected queries give pristine results -> drop test_execute_tpcds")
        else:
            why.append(f"TPC-DS probe: {len(qs)} queries, a result differs -> keep")


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
    tests_by_num = {t["number"]: t["id"] for t in tests}
    all_numbers = set(durations)
    try:
        if MAP_PICKLE.exists():
            import pickle

            with open(MAP_PICKLE, "rb") as f:
                mp = pickle.load(f)
        else:
            with gzip.open(MAP, "rt") as f:
                mp = json.load(f)
        sel = Selector(tests, mp)
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
        # DIAGNOSTIC BUILD ONLY (branch diag-no-tpcds): never run test_execute_tpcds, to
        # measure how many practice commits really need it. NOT a final candidate.
        chosen -= {n for n in chosen if tests_by_num[n] == "tests/test_executor.py::TestExecutor::test_execute_tpcds"}
        workers = split(chosen, durations)
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        log(f"{patch.stem}: {len(chosen)} tests, {len(workers)} workers | " + "; ".join(why))


if __name__ == "__main__":
    main()
