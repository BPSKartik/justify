"""
Stage 6 — proof.

Applies removals to a temporary copy and lets the project's own build and tests
decide. The repository itself is only ever read.

Edits are made on syntax trees, never by text replacement: an import statement
is regenerated from its own node with the dead names dropped, so a multi-line
import cannot lose a comma. A statement that was the only thing in its block is
replaced by `pass`, so removing it cannot leave an empty `try:` behind. A
statement that shares its line with other code (`import os; os.environ[...]`)
is not edited at all.

A pass only counts when it means something:

  * the unchanged copy must pass first (the baseline), or nothing can be proved;
  * the baseline run records which files the tests actually load — a removal in
    a file the tests never import "passes" vacuously, so it is not provable;
  * a run that passes with fewer tests passing, or more skipped, than the
    baseline is a failure (removing a fixture can silently skip tests);
  * removals that each pass alone are run again together before any is certified;
  * a file reached through a symlink is never written, so the edit cannot leak
    out of the copy.
"""

from __future__ import annotations

import ast
import io
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
import tokenize
from collections import defaultdict

from .model import Finding

PROVABLE_KINDS = {"import", "function", "class"}

# names of environment variables that hold secrets; they never reach the tests
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE|CREDENTIAL|^AZURE_|^AWS_|"
                        r"^GITHUB_|^ANTHROPIC_|^OPENAI_|^JUSTIFY_MCP", re.I)

# never copied: version control and caches. Matched as directories only — a file
# called .env or build is part of the project and the tests may need it.
CACHE_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
              ".justify", ".tox", ".nox"}

# loaded into every Python process the test command starts; records which files were imported
_TRACER = '''
import atexit, os, sys
def _justify_trace():
    d = os.environ.get("JUSTIFY_TRACE_DIR")
    if not d:
        return
    try:
        with open(os.path.join(d, "%d.txt" % os.getpid()), "w") as fh:
            for m in list(sys.modules.values()):
                f = getattr(m, "__file__", None)
                if f:
                    fh.write(os.path.realpath(f) + "\\n")
    except Exception:
        pass
atexit.register(_justify_trace)
'''


class SharedLine(ValueError):
    """The statement shares a physical line with other code; editing it would take that code too."""


def _bound(alias: ast.alias, node: ast.AST) -> str:
    if isinstance(node, ast.Import):
        return alias.asname or alias.name.split(".")[0]
    return alias.asname or alias.name


def _owns_its_lines(node: ast.AST, lines: list[str]) -> bool:
    first, last = lines[node.lineno - 1], lines[(node.end_lineno or node.lineno) - 1]
    before = first[: node.col_offset].strip()
    after = last[node.end_col_offset:].strip() if node.end_col_offset is not None else ""
    return not before and (not after or after.startswith("#"))


def edit_source(text: str, items: list[Finding]) -> str:
    """Return the source with the given imports/definitions removed. Raises SyntaxError
    if the result would not compile, SharedLine if a statement does not own its lines."""
    tree = ast.parse(text)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    lines = text.split("\n")

    spans: list[tuple[int, int, list[str]]] = []      # (start, end, replacement lines)

    imports_by_line: dict[int, set[str]] = defaultdict(set)
    defs = {(f.line, f.name) for f in items if f.kind != "import"}
    for f in items:
        if f.kind == "import":
            imports_by_line[f.line].add(f.name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            if (start, node.name) in defs:
                if not _owns_its_lines(node, lines):
                    raise SharedLine(f"{node.name} shares a line with other code")
                spans.append((start, node.end_lineno or node.lineno, []))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno in imports_by_line:
            dead = imports_by_line[node.lineno]
            keep = [a for a in node.names if _bound(a, node) not in dead]
            if len(keep) == len(node.names):
                continue
            if not _owns_its_lines(node, lines):
                raise SharedLine(f"the import on line {node.lineno} shares its line with other code")
            first = lines[node.lineno - 1]
            indent = first[: len(first) - len(first.lstrip())]
            if keep:
                new = type(node)(**{**{k: getattr(node, k) for k in node._fields}, "names": keep})
                repl = [indent + ast.unparse(new)]
            else:
                parent = parents.get(node)
                siblings = None
                for fld in ("body", "orelse", "finalbody", "handlers"):
                    seq = getattr(parent, fld, None)
                    if isinstance(seq, list) and node in seq:
                        siblings = seq
                repl = [indent + "pass"] if siblings is not None and len(siblings) == 1 \
                    and not isinstance(parent, ast.Module) else []
            spans.append((node.lineno, node.end_lineno or node.lineno, repl))

    # a span inside another removed span (an import inside a removed function) goes with it
    spans.sort(key=lambda s: (s[0], -s[1]))
    merged: list[tuple[int, int, list[str]]] = []
    for s in spans:
        if merged and s[0] <= merged[-1][1]:
            continue
        merged.append(s)
    for start, end, repl in reversed(merged):
        lines[start - 1: end] = repl

    out = "\n".join(lines)
    compile(out, "<edited>", "exec")
    return out


def read_source(path: pathlib.Path) -> tuple[str, str]:
    """Text and encoding, honouring a BOM or a coding cookie the way Python does."""
    raw = path.read_bytes()
    try:
        enc, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    except SyntaxError:
        enc = "utf-8"
    return raw.decode(enc, errors="replace"), enc


def _copy(root: pathlib.Path, dest: pathlib.Path) -> None:
    def ignore(dirpath: str, names: list[str]) -> set[str]:
        out = set()
        for n in names:
            p = os.path.join(dirpath, n)
            if os.path.isdir(p) and not os.path.islink(p):
                if n in CACHE_DIRS or n == "node_modules" or os.path.exists(os.path.join(p, "pyvenv.cfg")):
                    out.add(n)
        return out
    shutil.copytree(root, dest, ignore=ignore, symlinks=True)
    # a virtualenv or node_modules at the root is linked, not copied: the tests may need
    # it, and no edit ever touches it
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink() and not (dest / child.name).exists() \
                and (child.name == "node_modules" or (child / "pyvenv.cfg").exists()):
            (dest / child.name).symlink_to(child)


def _inside(copy_root: pathlib.Path, rel: str) -> bool:
    """True when the file is a real file inside the copy — not reached through any symlink."""
    p = copy_root
    for part in rel.split("/"):
        p = p / part
        if p.is_symlink():
            return False
    return p.is_file() and p.resolve().is_relative_to(copy_root.resolve())


_COUNTS = {
    "passed": re.compile(r"(\d+) passed"), "skipped": re.compile(r"(\d+) skipped"),
    "deselected": re.compile(r"(\d+) deselected"), "ran": re.compile(r"Ran (\d+) tests?"),
    "uskipped": re.compile(r"skipped=(\d+)"),
}


def _counts(output: str) -> dict[str, int]:
    out = {}
    for k, rx in _COUNTS.items():
        found = rx.findall(output)
        if found:
            out[k] = int(found[-1])
    return out


def _fewer_tests(base: dict[str, int], now: dict[str, int]) -> str | None:
    for k in ("passed", "ran"):
        if k in base and now.get(k, 0) < base[k]:
            return f"fewer tests ran ({base[k]} {k} before, {now.get(k, 0)} after)"
    for k in ("skipped", "deselected", "uskipped"):
        if now.get(k, 0) > base.get(k, 0):
            return f"more tests were skipped ({base.get(k, 0)} before, {now[k]} after)"
    return None


class _Runner:
    def __init__(self, command: str, copy_root: pathlib.Path, timeout: int, scratch: pathlib.Path):
        self.command, self.copy_root, self.timeout = command, copy_root, timeout
        self.tracer_dir = scratch / "tracer"
        self.tracer_dir.mkdir()
        (self.tracer_dir / "sitecustomize.py").write_text(_TRACER)
        self.trace_dir = scratch / "trace"
        self.baseline: dict[str, int] = {}

    def run(self, trace: bool = False) -> tuple[bool, str]:
        # the tests are the repository's code: they get no secrets (tokens, keys, passwords)
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
        # the copy comes first on the path, so an editable install of the original cannot
        # answer the imports in its place
        paths = [str(self.copy_root), str(self.copy_root / "src")]
        if trace:
            paths.insert(0, str(self.tracer_dir))
            self.trace_dir.mkdir(exist_ok=True)
            env["JUSTIFY_TRACE_DIR"] = str(self.trace_dir)
        env["PYTHONPATH"] = os.pathsep.join(paths + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        p = subprocess.Popen(self.command, shell=True, cwd=self.copy_root, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            # kill the whole process group: a test runner's children must not outlive the run
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.communicate()
            return False, f"tests did not finish within {self.timeout}s"
        r = p
        if r.returncode != 0:
            return False, out[-1200:]
        if self.baseline:
            fewer = _fewer_tests(self.baseline, _counts(out))
            if fewer:
                return False, fewer
        return True, out[-1200:]

    def loaded(self) -> set[str] | None:
        if not self.trace_dir.exists():
            return None
        seen: set[str] = set()
        for p in self.trace_dir.glob("*.txt"):
            seen.update(p.read_text().split("\n"))
        return seen or None


def prove(root: pathlib.Path, findings: list[Finding], command: str, timeout: int = 600,
          isolate_limit: int = 25) -> dict:
    todo = [f for f in findings if f.kind in PROVABLE_KINDS and (f.final or f.verdict) == "REMOVE"
            and f.proof == "not run"]
    summary = {"command": command, "candidates": len(todo), "passed": 0, "failed": 0}
    if not todo:
        return {**summary, "batch": "nothing to prove"}

    with tempfile.TemporaryDirectory(prefix="justify-proof-") as tmp:
        scratch = pathlib.Path(tmp)
        copy_root = scratch / root.name
        _copy(root, copy_root)
        runner = _Runner(command, copy_root, timeout, scratch)

        # ---- files that cannot be edited safely
        originals: dict[str, tuple[str, str]] = {}
        for f in list(todo):
            if not _inside(copy_root, f.file):
                f.proof = "not provable (reached through a symlink)"
                todo.remove(f)
            elif f.file not in originals:
                originals[f.file] = read_source(copy_root / f.file)

        # ---- the baseline: the unchanged copy must pass, and shows what the tests load
        ok, out = runner.run(trace=True)
        if not ok:
            for f in todo:
                f.proof = "not provable (the tests fail before any change)"
            return {**summary, "batch": "baseline failed", "batch_output": out[-400:]}
        runner.baseline = _counts(out)
        summary["baseline"] = runner.baseline
        loaded = runner.loaded()
        if loaded is not None:
            for f in list(todo):
                if os.path.realpath(copy_root / f.file) not in loaded:
                    f.proof = "not provable (the tests never load this file)"
                    todo.remove(f)
        else:
            summary["load_check"] = "the test command does not let us see which files it loads"

        def write(rel: str, text: str) -> None:
            (copy_root / rel).write_bytes(text.encode(originals[rel][1]))

        def apply(items: list[Finding]) -> None:
            by_file: dict[str, list[Finding]] = defaultdict(list)
            for f in items:
                by_file[f.file].append(f)
            for rel, fs in by_file.items():
                write(rel, edit_source(originals[rel][0], fs))

        def restore() -> None:
            for rel, (text, _) in originals.items():
                write(rel, text)

        def attempt(items: list[Finding]) -> tuple[bool, str]:
            restore()
            try:
                apply(items)
            except SyntaxError as exc:
                return False, f"compile error: {exc}"
            return runner.run()

        # ---- edits that cannot be made on their own lines are not attempted
        for f in list(todo):
            try:
                edit_source(originals[f.file][0], [f])
            except SharedLine as exc:
                f.proof = f"not provable ({exc})"
                todo.remove(f)
            except SyntaxError as exc:
                f.proof = f"compile error: {exc}"
                todo.remove(f)
        if not todo:
            return {**summary, "batch": "nothing the tests can prove"}

        kept: list[Finding] = []
        try:
            # ---- all together
            batch_ok, batch_out = attempt(todo)
            if batch_ok:
                for f in todo:
                    f.proof = "passed"
                return {**summary, "passed": len(todo), "batch": "passed"}

            # ---- one at a time, to find which removal was needed
            alone_ok: list[Finding] = []
            for i, f in enumerate(todo):
                if i >= isolate_limit:
                    f.proof = "not run (isolation limit)"
                    continue
                ok, out = attempt([f])
                if ok:
                    alone_ok.append(f)
                else:
                    f.proof = out if out.startswith("compile error") else \
                        "failed: " + out.strip().split("\n")[-1][:200]

            # ---- each passed alone; certify only what also passes together
            together_ok, _ = attempt(alone_ok) if alone_ok else (True, "")
            if together_ok:
                kept = alone_ok
            else:
                for f in alone_ok:
                    ok, out = attempt(kept + [f])
                    if ok:
                        kept.append(f)
                    else:
                        f.proof = "failed together with other removals: " + out.strip().split("\n")[-1][:160]
            for f in kept:
                f.proof = "passed"
        finally:
            restore()
        return {**summary, "passed": len(kept), "failed": sum(1 for f in todo if f.proof != "passed"),
                "batch": "failed — isolated each removal", "batch_output": batch_out[-400:]}
