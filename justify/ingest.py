"""
Stage 1 — ingest.

Reads every Python file once and fingerprints it. The fingerprint is what lets
a later run skip what has not changed: the ledger keeps each file's hash, so a
two-million-line repository is read in full once and then only where it moved.
"""

from __future__ import annotations

import ast
import hashlib
import io
import pathlib
import tokenize
from dataclasses import dataclass, field

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", "build", "dist",
    "site-packages", ".eggs", ".justify",
}


@dataclass
class SourceFile:
    path: pathlib.Path
    rel: str
    sha: str
    text: str
    lines: int
    tree: ast.Module | None = None
    error: str | None = None
    source_lines: list[str] = field(default_factory=list)


def is_skipped(rel_parts: tuple[str, ...]) -> bool:
    return any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel_parts)


def inside(root: pathlib.Path, path: pathlib.Path) -> bool:
    """True for a real file under root reached without any symlink. A repository can hold a
    link to /etc/passwd or to a secrets file next to it; following it would read — and echo in
    findings — something that is not the repository's code."""
    p = root
    for part in path.relative_to(root).parts:
        p = p / part
        if p.is_symlink():
            return False
    try:
        return path.is_file() and path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def ingest(root: pathlib.Path) -> list[SourceFile]:
    files: list[SourceFile] = []
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts
        if is_skipped(rel_parts) or not inside(root, path):
            continue
        raw = path.read_bytes()
        try:   # a BOM or a coding cookie decides the encoding, the way Python itself reads the file
            enc, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        except SyntaxError:
            enc = "utf-8"
        text = raw.decode(enc, errors="replace")
        sf = SourceFile(path=path, rel="/".join(rel_parts), sha=hashlib.sha256(raw).hexdigest()[:16],
                        text=text, lines=text.count("\n") + (0 if text.endswith("\n") or not text else 1),
                        source_lines=text.split("\n"))
        try:
            sf.tree = ast.parse(text, filename=str(path))
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            sf.error = f"{exc.__class__.__name__}: {str(exc)[:200]}"
            sf.tree = None
        files.append(sf)
    return files


BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".whl",
                   ".so", ".dylib", ".dll", ".exe", ".bin", ".pyc", ".db", ".sqlite", ".sqlite3", ".woff",
                   ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".mov", ".onnx", ".pt", ".pkl", ".npy", ".parquet",
                   ".lock"}


def other_files(root: pathlib.Path) -> list[tuple[str, str]]:
    """Every other text file in the repository: Dockerfiles, CI, scripts, config, Terraform,
    TypeScript, .kv layouts, docs. Any of them can name a Python function — a Lambda handler in
    main.tf, an entry point in a Procfile — and a name found there counts as a use."""
    out = []
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if path.suffix in (".py",) or path.suffix.lower() in BINARY_SUFFIXES or is_skipped(rel_parts):
            continue
        try:
            if not inside(root, path) or path.stat().st_size > 512_000:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue          # binary
        out.append(("/".join(rel_parts), raw.decode("utf-8", errors="replace")))
    return out
