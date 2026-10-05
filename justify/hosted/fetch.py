"""
Getting a public GitHub repository onto disk, safely.

Everything a visitor types is untrusted: the URL decides which host git talks to and the
ref is passed to git on a command line. So both are matched against strict patterns — only
https://github.com/<owner>/<repo>, only plain branch or tag names — before git sees them,
and git itself is configured so the clone cannot reach another protocol, run a hook,
create a symlink, ask for a password or pull LFS objects.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
NAME = r"[A-Za-z0-9._-]{1,100}"
# anything after owner/name — a page inside the repository, a ?query, a #fragment — is what people
# copy from the address bar; only /tree/<ref> and /blob/<ref>/... name something to audit
URL = re.compile(rf"^(?:https?://)?(?:www\.)?github\.com/({OWNER})/({NAME}?)(?:\.git)?(/[^?#]*)?(?:[?#].*)?$", re.I)
INSIDE = re.compile(r"^/(?:tree|blob)/(.+)$")
SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
SHORT = re.compile(rf"^({OWNER})/({NAME}?)(?:\.git)?$")
REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SHA = re.compile(r"^[0-9a-f]{40}$")

GIT = ["git", "-c", "core.symlinks=false", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
       "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "submodule.recurse=false",
       "-c", "core.fsmonitor=false", "-c", "advice.detachedHead=false"]


class FetchError(Exception):
    """A problem a visitor can understand, with the HTTP status that says what kind."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str
    ref: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"

    @property
    def clone_url(self) -> str:
        return f"{self.url}.git"


def parse(repo: str, ref: str = "") -> RepoRef:
    text = (repo or "").strip()
    if not text or len(text) > 300 or any(ord(c) < 33 or ord(c) == 127 for c in text):
        raise FetchError("Paste a GitHub repository address, like https://github.com/owner/name.")
    m = URL.match(text) or SHORT.match(text)
    if not m:
        raise FetchError("Only public GitHub repositories can be scanned here: https://github.com/owner/name.")
    owner, name = m.group(1), m.group(2)
    if name in (".", "..") or name.endswith(".") or owner.endswith("-"):
        raise FetchError("That is not a valid GitHub repository name.")
    if not ref and m.re is URL and (inside := INSIDE.match(m.group(3) or "")):
        parts = []
        for seg in inside.group(1).split("/"):     # a branch, then perhaps a path: keep what can be a name
            if not SEGMENT.match(seg) or seg == "..":
                break
            parts.append(seg)
        ref = "/".join(parts)[:200]
    ref = (ref or "").strip().strip("/")
    if ref:
        bad = (not REF.match(ref) or ref.startswith(("-", ".", "/")) or ".." in ref or "//" in ref
               or ref.endswith((".lock", "/", ".")) or "@{" in ref)
        if bad:
            raise FetchError("That branch or tag name is not valid.")
    return RepoRef(owner, name, ref)


def _env(home: str, token: str | None = None) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LANG": "C.UTF-8",
           "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"}
    if token:
        # a GitHub App installation token, passed in the environment — never on a command line other
        # processes could read, never written into the clone's own config
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"})
    return env


def _prefixes(ref: str) -> list[str]:
    """main/src/app.py from a /tree or /blob address: the branch is some leading part of it."""
    parts = ref.split("/")
    return ["/".join(parts[:n]) for n in range(len(parts), 0, -1)]


def _pick(found: dict[str, str], ref: str) -> tuple[str, str] | None:
    """The longest leading part of `ref` that is a branch or tag, and its commit."""
    for cand in _prefixes(ref):
        for name in (f"refs/heads/{cand}", f"refs/tags/{cand}^{{}}", f"refs/tags/{cand}"):
            if SHA.match(found.get(name, "")):
                return found[name], cand
    return None


def resolve(rr: RepoRef, timeout: int = 30, token: str | None = None) -> tuple[str, str]:
    """The commit to scan and the branch or tag it came from, without downloading anything."""
    with tempfile.TemporaryDirectory(prefix="justify-ls-") as home:
        patterns = [p for cand in _prefixes(rr.ref) for p in
                    (f"refs/heads/{cand}", f"refs/tags/{cand}", f"refs/tags/{cand}^{{}}")] if rr.ref else []
        args = GIT + ["ls-remote", "--symref", "--", rr.clone_url, "HEAD"] if not rr.ref else \
            GIT + ["ls-remote", "--", rr.clone_url, *patterns]
        try:
            r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout, env=_env(home, token))
        except subprocess.TimeoutExpired:
            raise FetchError("GitHub did not answer in time. Try again in a minute.", 504, "timeout") from None
    if r.returncode != 0:
        said = (r.stderr or "").lower()
        if "not found" in said or "could not read username" in said or "authentication" in said:
            raise FetchError(f"{rr.slug} was not found, or it is private. The hosted service scans public "
                             "repositories; for private code, install Justify locally.", 404, "not_found")
        raise FetchError("Could not reach GitHub. Try again in a minute.", 502, "upstream")
    lines = [ln.split("\t") for ln in r.stdout.strip().splitlines() if "\t" in ln]
    if not rr.ref:
        branch = next((ln.split("refs/heads/", 1)[-1] for ln in r.stdout.splitlines()
                       if ln.startswith("ref: refs/heads/")), "")
        branch = branch.split("\t")[0].strip()
        sha = next((sha for sha, name in lines if name == "HEAD" and SHA.match(sha)), "")
        if not SHA.match(sha):
            raise FetchError(f"{rr.slug} has no commits to scan.", 404, "empty")
        return sha, branch or "HEAD"
    hit = _pick({name: sha for sha, name in lines}, rr.ref)
    if hit:
        return hit
    raise FetchError(f"{rr.slug} has no branch or tag named “{rr.ref}”.", 404, "ref_not_found")


_SIZES: dict[str, tuple[float, int]] = {}


def _github_kb(rr: RepoRef, timeout: float) -> int | None:
    """GitHub's own figure for the repository's size, or None when it cannot say."""
    import json
    import urllib.request

    from ..llm import _ssl_context
    req = urllib.request.Request(f"https://api.github.com/repos/{rr.owner}/{rr.name}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "justify"})
    cid, secret = os.environ.get("JUSTIFY_GITHUB_CLIENT_ID"), os.environ.get("JUSTIFY_GITHUB_CLIENT_SECRET")
    if cid and secret:                    # the sign-in app's own allowance: 5,000 an hour, not 60
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
            size = json.load(r).get("size")
    except (OSError, ValueError):
        return None
    return size if isinstance(size, int) else None


def check_size(rr: RepoRef, max_mb: int, timeout: float = 5.0) -> None:
    """Refuse a repository GitHub already says is far too large, before it is queued or charged.
    GitHub counts every blob of every commit and the clone leaves those over 1 MB behind — React is
    1,075 MB on GitHub and a 201 MB clone — so only one far past the limit is refused here; the
    clone still watches its own size. No answer: no refusal."""
    key, now = rr.slug.lower(), time.monotonic()
    hit = _SIZES.get(key)
    kb = hit[1] if hit and now - hit[0] < 3600 else _github_kb(rr, timeout)
    if kb is None:
        return
    if len(_SIZES) > 5000:
        _SIZES.clear()
    _SIZES[key] = (hit[0] if hit and now - hit[0] < 3600 else now, kb)
    if kb > max_mb * 1024 * 5:
        raise FetchError(f"{rr.slug} is about {kb // 1024:,} MB on GitHub — too large for the hosted service. "
                         "Install Justify on your own machine to audit it.", 413, "too_large")


def _size_mb(path: str) -> float:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total / 1_048_576


def clone(rr: RepoRef, branch: str, dest: str, timeout: int = 180, max_mb: int = 400, token: str | None = None) -> str:
    """Full history (attribution needs every commit), blobs over 1 MB left on the server, the
    size watched while it downloads. Returns the commit actually checked out."""
    home = tempfile.mkdtemp(prefix="justify-git-")
    args = GIT + ["clone", "--filter=blob:limit=1m", "--no-tags", "--single-branch", "--quiet"]
    if branch and branch != "HEAD":
        args += ["--branch", branch]
    args += ["--", rr.clone_url, dest]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", env=_env(home, token),
                         start_new_session=True)
    start = time.monotonic()
    try:
        while p.poll() is None:
            if time.monotonic() - start > timeout:
                _kill(p)
                raise FetchError(f"Downloading {rr.slug} took longer than {timeout} s.", 504, "clone_timeout")
            if os.path.isdir(dest) and _size_mb(dest) > max_mb:
                _kill(p)
                raise FetchError(f"{rr.slug} is larger than {max_mb} MB, the limit for the hosted service. "
                                 "Install Justify locally to scan it.", 413, "too_large")
            time.sleep(0.5)
        if p.returncode != 0:
            err = (p.stderr.read() if p.stderr else "") or ""
            if "not found" in err.lower():
                raise FetchError(f"{rr.slug} was not found, or it is private.", 404, "not_found")
            raise FetchError("GitHub refused the download. Try again in a minute.", 502, "clone_failed")
        r = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"], capture_output=True, text=True, errors="replace",
                           timeout=10, env=_env(home))
        return r.stdout.strip()
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    p.wait()
