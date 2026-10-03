"""
Who wrote it — the part that answers the problem statement.

"Does the AI assistant actually pay off?" is a question about the assistant,
not about code quality in general. So every line is attributed, from the git
history, to an AI-assisted commit or a human one, and the dead weight is split
the same way. That turns "6 dead imports" into "AI-assisted lines carry N times
the dead weight of human lines in this repository" — a statement about the
assistant.

A commit counts as AI-assisted when its message carries a trailer that the
assistants themselves write: "Co-Authored-By: Claude …", "Co-authored-by:
Copilot …", "Generated with Claude Code", and similar. That is a lower bound —
an assistant used without a trailer is counted as human — and the report says so.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
from collections import Counter

DEFAULT_PATTERNS = [
    r"co-authored-by:[^\n]*(claude|copilot|chatgpt|gpt-?\d|openai|codex|cursor|gemini|devin|aider|"
    r"windsurf|codeium|amazon q|codewhisperer|tabnine|anthropic)",
    r"generated (with|by) \[?(claude|copilot|chatgpt|cursor|codex|gemini)",
    r"noreply@anthropic\.com",
    r"assisted-by:",
    r"ai-generated",
]


def _patterns() -> list[re.Pattern]:
    extra = [p for p in os.environ.get("JUSTIFY_AI_PATTERNS", "").split("||") if p.strip()]
    return [re.compile(p, re.I) for p in DEFAULT_PATTERNS + extra]


def _git(root: pathlib.Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


class Attribution:
    """Line-level authorship for one repository, cached per commit."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        top = _git(root, "rev-parse", "--show-toplevel")
        tracked = _git(root, "ls-files", "--", "*.py") if top else None
        # a folder inside some other repository (a home directory under git, say) is not
        # attributed unless its own Python files are tracked
        self.enabled = bool(top and tracked and tracked.strip())
        self.top = pathlib.Path(top.strip()) if top else None
        self._commit_kind: dict[str, str] = {}
        self._commit_time: dict[str, int] = {}
        self._blame: dict[str, list[str]] = {}        # rel -> commit sha of every line
        self.pats = _patterns()

    def _load_all_commits(self) -> None:
        """Classify every commit with one `git log`, instead of one `git show` per commit —
        the difference between seconds and minutes on a repository with thousands of commits."""
        if self._commit_kind:
            return
        out = _git(self.root, "log", "--all", "--format=%H%x00%ct%x00%B%x00%an <%ae>%x01") or ""
        for entry in out.split("\x01"):
            entry = entry.strip("\n")
            if not entry:
                continue
            sha, _, rest = entry.partition("\x00")
            when, _, rest = rest.partition("\x00")
            sha = sha.strip()
            self._commit_kind[sha] = "ai" if any(p.search(rest) for p in self.pats) else "human"
            self._commit_time[sha] = int(when) if when.isdigit() else 0

    def _kind_of(self, sha: str) -> str:
        if sha.startswith("0000000"):
            return "uncommitted"
        self._load_all_commits()
        if sha not in self._commit_kind:      # e.g. a shallow clone's boundary commit
            msg = _git(self.root, "show", "-s", "--format=%B%n%an <%ae>", sha) or ""
            self._commit_kind[sha] = "ai" if any(p.search(msg) for p in self.pats) else "human"
        return self._commit_kind[sha]

    def _file_shas(self, rel: str) -> list[str]:
        """The commit that wrote every line of a file: index 0 is line 1."""
        if rel in self._blame:
            return self._blame[rel]
        path = (self.root / rel).resolve()
        out = _git(self.top or self.root, "blame", "--porcelain", "--", str(path))
        shas: list[str] = []
        if out:
            for line in out.split("\n"):
                m = re.match(r"^([0-9a-f]{40}) \d+ (\d+)", line)
                if m:
                    sha, final_line = m.group(1), int(m.group(2))
                    while len(shas) < final_line:
                        shas.append("")
                    shas[final_line - 1] = sha
        self._blame[rel] = shas
        return shas

    def _file_blame(self, rel: str) -> list[str]:
        """Kind for every line of a file: index 0 is line 1."""
        return [self._kind_of(s) if s else "unknown" for s in self._file_shas(rel)]

    def span(self, rel: str, start: int, end: int) -> str:
        if not self.enabled:
            return "unknown"
        kinds = self._file_blame(rel)[start - 1: end]
        if not kinds:
            return "uncommitted"
        c = Counter(kinds)
        return c.most_common(1)[0][0]

    def repo_lines(self, files: list[str]) -> Counter:
        """How many surviving lines each kind of commit wrote, across these files."""
        total = Counter()
        if not self.enabled:
            return total
        for rel in files:
            total.update(self._file_blame(rel))
        return total

    def rework(self, files: list[str]) -> dict | None:
        """Round 1's measure, kept: of the lines each kind of commit added, how many were later
        rewritten or deleted. Counted only from the first AI-assisted commit onwards, so human
        code from years before the assistant arrived — which has had longer to change — does
        not make the comparison unfair."""
        if not self.enabled:
            return None
        self._load_all_commits()
        out = _git(self.root, "log", "HEAD", "--no-merges", "--numstat", "--format=%x01%H", "--", "*.py")
        if not out:
            return None
        history = [c for c in out.split("\x01") if c.strip()]
        ai_times = [self._commit_time.get(c.split("\n", 1)[0].strip(), 0) for c in history
                    if self._commit_kind.get(c.split("\n", 1)[0].strip()) == "ai"]
        if not ai_times:
            return None
        since = min(ai_times)
        wanted = set(files)
        added: Counter = Counter()
        for c in history:
            sha, _, body = c.partition("\n")
            sha = sha.strip()
            if self._commit_time.get(sha, 0) < since:
                continue
            for row in body.split("\n"):
                parts = row.split("\t")
                if len(parts) == 3 and parts[0].isdigit():
                    added[self._commit_kind.get(sha, "human")] += int(parts[0])
        surviving: Counter = Counter()
        for rel in files if wanted else []:
            for sha in self._file_shas(rel):
                if sha and self._commit_time.get(sha, 0) >= since:
                    surviving[self._kind_of(sha)] += 1
        import datetime as _dt
        res = {"since": _dt.datetime.fromtimestamp(since).strftime("%Y-%m-%d")}
        for kind in ("ai", "human"):
            a, sv = added[kind], min(surviving[kind], added[kind])
            res[kind] = {"added": a, "surviving": sv,
                         "rewritten_percent": round(100.0 * (a - sv) / a, 1) if a else None}
        return res

    def ai_commits(self) -> tuple[int, int]:
        out = _git(self.root, "log", "--format=%H")
        if not out:
            return 0, 0
        self._load_all_commits()
        shas = [s for s in out.split() if s]
        return sum(1 for s in shas if self._commit_kind.get(s) == "ai"), len(shas)
