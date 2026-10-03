"""
Code people upload: a .zip, a folder, or files pasted into an AI chat (over MCP).

An upload is a stranger's archive, so it is unpacked as if it were hostile: no absolute paths,
no `..`, no symlinks, no devices; the bytes actually written are counted (a zip can lie about
its sizes), and both the file count and the total are capped.

A `.git` folder inside the archive is the one way private code can bring its history, so the
part git needs to read history is kept — objects, refs, HEAD — and everything that could tell
git to run something (config, hooks, info/, alternates) is dropped. Justify writes the config.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import stat
import tempfile
import zipfile

from .fetch import FetchError

MAX_ZIP_BYTES = 25 * 1024 * 1024
MAX_UNPACKED_BYTES = 150 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_JSON_FILES = 2_000
MAX_JSON_BYTES = 8 * 1024 * 1024
GIT_KEEP = re.compile(r"^\.git/(HEAD|packed-refs|shallow|refs/.+|objects/(?!info/).+)$")
GIT_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfilemode = false\n"


def clean_name(name: str) -> str:
    """A display name for an upload: letters, digits, dot, dash, underscore."""
    base = re.sub(r"\.zip$", "", os.path.basename(name or "").strip(), flags=re.I)
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")[:60]
    return base or "code"


def _safe_rel(name: str) -> str | None:
    """A relative path inside the archive, or None if it tries to leave it."""
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or "\0" in name:
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or len(name) > 500:
        return None
    return "/".join(parts)


def _strip_common_root(names: list[str]) -> str:
    """A zip of a folder puts everything under that folder's name; audit what is inside it."""
    tops = {n.split("/", 1)[0] for n in names if n}
    if len(tops) == 1 and all("/" in n for n in names):
        return tops.pop() + "/"
    return ""


def _write(dest: str, rel: str, data: bytes) -> None:
    path = os.path.join(dest, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _finish(dest: str, wrote_git: bool) -> None:
    if wrote_git:
        if os.path.exists(os.path.join(dest, ".git", "HEAD")):
            with open(os.path.join(dest, ".git", "config"), "w") as fh:
                fh.write(GIT_CONFIG)
        else:
            shutil.rmtree(os.path.join(dest, ".git"), ignore_errors=True)


def unpack_zip(data: bytes, root: str) -> str:
    """Unpack an uploaded zip into a new folder under `root`. Returns the folder."""
    if len(data) > MAX_ZIP_BYTES:
        raise FetchError(f"That zip is larger than {MAX_ZIP_BYTES // 1048576} MB.", 413, "too_large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise FetchError("That is not a zip file Justify can open.", 400, "bad_zip") from None
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise FetchError(f"That zip holds more than {MAX_ENTRIES:,} entries.", 413, "too_many_files")
    names = [(_safe_rel(i.filename), i) for i in infos]
    if any(rel is None for rel, _ in names):
        raise FetchError("That zip has a path that points outside itself. Justify will not unpack it.", 400, "bad_zip")
    prefix = _strip_common_root([rel for rel, i in names if not i.is_dir()])
    dest = tempfile.mkdtemp(prefix="up-", dir=root)
    total, wrote_git = 0, False
    try:
        for rel, info in names:
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if (mode & 0o170000) and not stat.S_ISREG(mode):
                continue                                  # symlinks, devices, sockets: never written
            if prefix:
                if not rel.startswith(prefix):
                    continue
                rel = rel[len(prefix):]
            if rel == ".git" or rel.startswith(".git/"):
                if not GIT_KEEP.match(rel):
                    continue
                wrote_git = True
            elif "/.git/" in f"/{rel}" or rel.startswith("__MACOSX/"):
                continue                                  # nested repositories and Finder litter
            with zf.open(info) as src:
                chunk = src.read(MAX_UNPACKED_BYTES - total + 1)
            total += len(chunk)
            if total > MAX_UNPACKED_BYTES:
                raise FetchError(f"Unpacked, that zip is larger than {MAX_UNPACKED_BYTES // 1048576} MB.",
                                 413, "too_large")
            _write(dest, rel, chunk)
        _finish(dest, wrote_git)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def unpack_files(files: list, root: str) -> str:
    """Write `[{"path": ..., "content": ...}]` — files from a browser folder pick, or pasted into
    an AI chat — into a new folder under `root`."""
    if not isinstance(files, list) or not files:
        raise FetchError("Send at least one file: [{\"path\": \"app.py\", \"content\": \"...\"}].", 400, "no_files")
    if len(files) > MAX_JSON_FILES:
        raise FetchError(f"Send at most {MAX_JSON_FILES:,} files at once, or a zip.", 413, "too_many_files")
    dest = tempfile.mkdtemp(prefix="up-", dir=root)
    total = 0
    try:
        rels = []
        for f in files:
            if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("content"), str):
                raise FetchError("Each file needs a text \"path\" and \"content\".", 400, "bad_files")
            rel = _safe_rel(f["path"])
            if rel is None or rel == ".git" or rel.startswith(".git/") or "/.git/" in f"/{rel}":
                raise FetchError(f"That path is not allowed: {f['path'][:80]}", 400, "bad_files")
            rels.append((rel, f["content"].encode("utf-8", errors="replace")))
        prefix = _strip_common_root([r for r, _ in rels])
        for rel, data in rels:
            total += len(data)
            if total > MAX_JSON_BYTES:
                raise FetchError(f"Those files add up to more than {MAX_JSON_BYTES // 1048576} MB. Send a zip.",
                                 413, "too_large")
            _write(dest, rel[len(prefix):] if prefix else rel, data)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest
