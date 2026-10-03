"""
Who wrote it — the part that answers the problem statement.

"Does the AI assistant actually pay off?" is a question about the assistant,
not about code quality in general. So every line is attributed, from the git
history, to an AI-assisted commit or a human one, and the dead weight is split
the same way. That turns "6 dead imports" into "AI-assisted lines carry N times
the dead weight of human lines in this repository" — a statement about the
assistant.

A commit counts as AI-assisted when it carries a signature an assistant leaves
behind: a trailer it writes ("Co-Authored-By: Claude …", "Assisted-by: …"), a
"Generated with …" line, or the bot account an agent commits as (Copilot coding
agent, Devin, Jules, Cursor Agent, Aider's "(aider)" author name, and others).
Signatures are only read where an assistant writes them — trailers and commit
identities — so a commit message that merely mentions "cursor" is not one.

That is a lower bound. Code pasted from a chat window carries no signature, so it
counts as "no AI trace", never as proof a person wrote it — and the report says so.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
from collections import Counter

# (assistant, how its name appears inside a trailer, a "Generated with" line or a commit identity)
TOOLS: list[tuple[str, str]] = [
    # Claude is also a person's name: only the assistant's own forms count
    ("Claude", r"anthropic|^claude(\s+(code|opus|sonnet|haiku|fable|\d[\w.]*))*\s*(<|$)|claude\[bot\]"),
    ("GitHub Copilot", r"copilot"),
    ("OpenAI Codex / ChatGPT", r"codex|chatgpt|openai|\bgpt-?\d"),
    ("Cursor", r"cursor"),
    ("Gemini / Jules", r"gemini|google-labs-jules|jules\[bot\]"),
    ("Devin", r"devin-ai|devin\[bot\]|cognition"),
    ("Aider", r"aider"),
    ("Windsurf", r"windsurf|codeium"),
    ("Amazon Q", r"amazon[ -]?q\b|codewhisperer"),
    ("Tabnine", r"tabnine"),
    ("Lovable", r"lovable|gpt-engineer"),
    ("v0", r"\bv0\b"),
    ("Bolt", r"bolt\.new|stackblitz"),
    ("Replit Agent", r"replit"),
    ("Cline / Roo Code", r"\bcline\b|roo[ -]?code"),
    ("Kiro", r"\bkiro\b"),
    ("OpenHands", r"openhands|all-hands\.dev"),
    ("Sweep", r"sweep-ai|sweep\[bot\]"),
    ("Codegen", r"codegen-sh|codegen\.com"),
]
_TOOL_RES = [(name, re.compile(rx, re.I)) for name, rx in TOOLS]

# where an assistant signs a commit: trailers it writes, and the line Claude Code / others add
_TRAILER = re.compile(r"^[ \t]*(co-authored-by|assisted-by|generated-by|ai-assisted-by|made-with|"
                      r"written-by|ai-agent)[ \t]*:[ \t]*(.+)$", re.I | re.M)
_GENERATED = re.compile(r"generated (?:with|by|using)\s+\[?([^\]\n(]{2,60})", re.I)
# the accounts agents commit as; an identity is "name <email>" for the author or the committer
_AGENT_IDENTITY = re.compile(
    r"copilot-swe-agent|^copilot <|devin-ai-integration|google-labs-jules|jules\[bot\]|lovable-dev|"
    r"gpt-engineer-app|\bv0\[bot\]|cursoragent|cursor agent|\(aider\)|openhands|sweep-ai|codegen-sh|"
    r"claude\[bot\]|chatgpt-codex-connector|amazon-q-developer|gemini-code-assist|replit-agent|"
    r"noreply@anthropic\.com", re.I)


def _tool_in(text: str) -> str | None:
    for name, rx in _TOOL_RES:
        if rx.search(text):
            return name
    return None


def classify(message: str, author: str = "", committer: str = "",
             extra: list[re.Pattern] | None = None) -> tuple[str, str | None]:
    """("ai", assistant) or ("human", None) for one commit. Only places an assistant writes
    are read — trailers, a "Generated with" line, the author and committer identities."""
    for m in _TRAILER.finditer(message):
        tool = _tool_in(m.group(2))
        if tool:
            return "ai", tool
        if m.group(1).lower() in ("assisted-by", "generated-by", "ai-assisted-by", "ai-agent"):
            return "ai", "Other assistant"
    for m in _GENERATED.finditer(message):
        tool = _tool_in(m.group(1))
        if tool:
            return "ai", tool
    for ident in (author, committer):
        if ident and _AGENT_IDENTITY.search(ident):
            return "ai", _tool_in(ident) or "Other assistant"
    if re.search(r"noreply@anthropic\.com", message, re.I):
        return "ai", "Claude"
    if re.search(r"^aider: ", message, re.I | re.M):
        return "ai", "Aider"
    if re.search(r"\bai-generated\b", message, re.I):
        return "ai", "Other assistant"
    for rx in extra or ():
        if rx.search(message) or rx.search(author) or rx.search(committer):
            return "ai", "Custom pattern"
    return "human", None


def _patterns() -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in os.environ.get("JUSTIFY_AI_PATTERNS", "").split("||") if p.strip()]


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
        tracked = _git(root, "ls-files", "--", ".") if top else None
        # a folder inside some other repository (a home directory under git, say) is not
        # attributed unless its own files are tracked
        self.enabled = bool(top and tracked and tracked.strip())
        self.top = pathlib.Path(top.strip()) if top else None
        self._commit_kind: dict[str, str] = {}
        self._commit_tool: dict[str, str] = {}
        self._commit_time: dict[str, int] = {}
        self._blame: dict[str, list[str]] = {}        # rel -> commit sha of every line
        self.pats = _patterns()

    def _load_all_commits(self) -> None:
        """Classify every commit with one `git log`, instead of one `git show` per commit —
        the difference between seconds and minutes on a repository with thousands of commits."""
        if self._commit_kind:
            return
        out = _git(self.root, "log", "--all", "--format=%H%x00%ct%x00%an <%ae>%x00%cn <%ce>%x00%B%x01") or ""
        for entry in out.split("\x01"):
            entry = entry.strip("\n")
            if not entry:
                continue
            sha, when, author, committer, message = (entry.split("\x00", 4) + ["", "", "", ""])[:5]
            sha = sha.strip()
            self._remember(sha, *classify(message, author, committer, self.pats))
            self._commit_time[sha] = int(when) if when.isdigit() else 0

    def _remember(self, sha: str, kind: str, tool: str | None) -> None:
        self._commit_kind[sha] = kind
        if tool:
            self._commit_tool[sha] = tool

    def _kind_of(self, sha: str) -> str:
        if sha.startswith("0000000"):
            return "uncommitted"
        self._load_all_commits()
        if sha not in self._commit_kind:      # e.g. a shallow clone's boundary commit
            out = _git(self.root, "show", "-s", "--format=%an <%ae>%x00%cn <%ce>%x00%B", sha) or ""
            author, committer, message = (out.split("\x00", 2) + ["", ""])[:3]
            self._remember(sha, *classify(message, author, committer, self.pats))
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

    def prefetch(self, files: list[str]) -> None:
        """Blame many files at once. git blame is one process per file and most of its time is
        spent waiting, so running them side by side turns minutes into seconds on a big repository."""
        if not self.enabled:
            return
        self._load_all_commits()            # before the threads: they only read it
        todo = [f for f in files if f not in self._blame]
        if not todo:
            return
        from concurrent.futures import ThreadPoolExecutor
        workers = int(os.environ.get("JUSTIFY_BLAME_WORKERS") or min(16, (os.cpu_count() or 4) * 2))
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            list(pool.map(self._file_shas, todo))

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

    def tools(self) -> dict[str, int]:
        """Which assistants signed the commits on the current branch, and how many each."""
        out = _git(self.root, "log", "--format=%H")
        if not out:
            return {}
        self._load_all_commits()
        c = Counter(self._commit_tool[s] for s in out.split() if s in self._commit_tool)
        return dict(c.most_common())

    def history_shape(self, files: list[str]) -> dict | None:
        """How the code that exists today arrived, read from the blame already taken (no extra git
        call — a diff of every commit would fetch every large file a partial clone left behind).
        When one commit wrote most of today's lines, the history cannot say how they were written,
        and the page says so plainly rather than report "100% human"."""
        c: Counter = Counter()
        for rel in files:
            for sha in self._file_shas(rel):
                if sha:
                    c[sha] += 1
        total = sum(c.values())
        if not total:
            return None
        biggest = c.most_common(1)[0][1]
        out = _git(self.root, "rev-list", "--count", "HEAD")
        commits = int(out.strip()) if out and out.strip().isdigit() else len(c)
        shallow = (_git(self.root, "rev-parse", "--is-shallow-repository") or "").strip() == "true"
        return {"commits": commits, "lines_today": total, "largest_commit_lines": biggest,
                "largest_commit_percent": round(100.0 * biggest / total, 1),
                "thin": commits <= 2 or biggest / total >= 0.8, "shallow": shallow}

    def ai_commits(self) -> tuple[int, int]:
        out = _git(self.root, "log", "--format=%H")
        if not out:
            return 0, 0
        self._load_all_commits()
        shas = [s for s in out.split() if s]
        return sum(1 for s in shas if self._commit_kind.get(s) == "ai"), len(shas)
