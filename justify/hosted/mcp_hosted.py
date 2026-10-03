"""
The public MCP endpoint: the tools an AI assistant calls to audit code for the person using it.

It reads code only — it never runs a repository's code or tests, and never calls a model. Calls
wait up to WAIT_S for a scan to finish (assistants give a tool a couple of minutes at most) and
otherwise hand back a scan_id to ask about again. Results are trimmed to stay well under what an
assistant accepts in one tool result, and they come with a fix plan: what to change, file by
file, in the order an assistant should do it — the assistant and the person decide the rest.

When accounts are switched on, every call carries the person's token (OAuth, or a personal
token from the dashboard): audits count against their allowance and land in their history,
and uploaded code stays visible to them alone.
"""

from __future__ import annotations

import os
import time

import anyio
from mcp.server.mcpserver import Context, MCPServer

try:
    from mcp.types import ToolAnnotations
except ImportError:                      # pragma: no cover
    ToolAnnotations = None

from .accounts import QuotaExceeded
from .fetch import FetchError, parse
from .ratelimit import client_ip
from .uploads import clean_name, unpack_files

WAIT_S = 110
TOP_FINDINGS = 60
ORDER = {"REMOVE": 0, "SIMPLIFY": 1, "AMBIGUOUS": 2, "KEEP": 3}

INSTRUCTIONS = (
    "Justify audits code for lines that cannot justify their existence — unused imports, functions, classes and "
    "dependencies, and duplicated code — and says how much came from AI-signed commits. Python gets the full dead-code "
    "audit; every other language is checked for copied blocks, and every repository gets a census of what it is made "
    "of. Use scan_github_repo for a public GitHub repository, audit_code for files the user shares with you (their own "
    "code, kept private to their account), get_scan_result to pick up a scan that was still running, and my_audits for "
    "the user's recent audits and what changed since the last one. This server only reads code: a REMOVE is a "
    "candidate no use was found for, not yet proved by tests. Follow the fix_plan, show the user each change before "
    "making it, and run their tests afterwards. To prove removals with the project's own tests, the user installs "
    "Justify locally: pip install \"justify-code[mcp]\".")


def _ann(read_only: bool = True):
    return ToolAnnotations(readOnlyHint=read_only, destructiveHint=False, idempotentHint=True,
                           openWorldHint=True) if ToolAnnotations else None


def fix_plan(findings: list[dict], blob_base: str = "") -> list[dict]:
    """What an assistant should change, grouped by file, removals first. Each step names the lines."""
    by_file: dict[str, list[dict]] = {}
    for f in sorted(findings, key=lambda f: (f["file"], ORDER.get(f["verdict"], 9), f["line"])):
        if f["verdict"] == "REMOVE":
            what = "dependency" if f["kind"] == "dependency" else f["kind"]
            step = (f"Delete {what} `{f['name']}` (lines {f['line']}–{f.get('end_line') or f['line']}): no use was "
                    "found anywhere in the repository. Search for dynamic uses (getattr, strings, plugins) first.")
        elif f["verdict"] == "SIMPLIFY":
            step = f"Merge `{f['name']}` (lines {f['line']}–{f.get('end_line') or f['line']}): {f.get('reason', '')}"
        elif f["verdict"] == "AMBIGUOUS":
            step = (f"Ask the user about {f['kind']} `{f['name']}` (line {f['line']}): {f.get('reason', '')} "
                    "— Justify will not call this either way.")
        else:
            continue
        by_file.setdefault(f["file"], []).append(
            {"step": step[:400], **({"link": f"{blob_base}{f['file']}#L{f['line']}"} if blob_base else {})})
    return [{"file": k, "changes": v[:12]} for k, v in list(by_file.items())[:25]]


def compact(scan: dict, base_url: str) -> dict:
    """A scan as a tool result: the numbers, the strongest findings, a fix plan and a link to the rest."""
    out = {"scan_id": scan["id"], "repo": scan["repo"], "commit": scan.get("sha"), "status": scan["status"],
           "private": scan.get("private", False),
           "full_report": f"{base_url}/s/{scan['id']}" if base_url else f"/s/{scan['id']}"}
    if scan["status"] == "failed":
        out["error"] = scan.get("error")
        return out
    if scan["status"] != "done":
        out["stage"] = (scan.get("stage") or {}).get("message")
        out["next"] = "Still running. Call get_scan_result with this scan_id in a little while."
        return out
    res = scan["result"]
    m = res.get("metrics", {})
    a = m.get("attribution") or {}
    counts: dict[str, int] = {}
    for f in res.get("findings", []):          # read-only audit: no judge, no proof — the verdict is the answer
        counts[f["verdict"]] = counts.get(f["verdict"], 0) + 1
    ranked = sorted(res.get("findings", []), key=lambda f: (ORDER.get(f["verdict"], 9), f["file"], f["line"]))
    base = res.get("github_blob_base", "")
    code = a.get("all_code") or {}
    hist = a.get("history") or {}
    out.update({
        "languages": [{"language": l["name"], "files": l["files"], "lines": l["lines"], "audited_for": l["audit"]}
                      for l in (res.get("languages") or [])[:8]],
        "python_files": res.get("files"), "python_lines": res.get("lines"),
        "justified_line_ratio_percent": m.get("jlr_percent"),
        "dead_weight_lines": m.get("dead_weight_lines"), "dead_weight_per_1000_lines": m.get("per_1000_lines"),
        "duplicate_lines": m.get("duplicate_lines"),
        "counts": counts,
        "authorship": {"lines_from_ai_signed_commits": code.get("ai_lines", a.get("ai_lines")),
                       "lines_with_no_ai_trace": code.get("human_lines", a.get("human_lines")),
                       "ai_commits": a.get("ai_commits"), "commits": a.get("commits"),
                       "assistants_seen": a.get("tools") or {},
                       "ai_dead_per_1000": a.get("ai_dead_per_1000"), "human_dead_per_1000": a.get("human_dead_per_1000"),
                       "history_is_thin": bool(hist.get("thin"))} if a else None,
        "findings": [{"verdict": f["verdict"], "kind": f["kind"], "file": f["file"], "line": f["line"],
                      "end_line": f.get("end_line"), "name": f["name"], "reason": (f.get("reason") or "")[:200],
                      "written_by": f.get("authored_by"),
                      **({"link": f"{base}{f['file']}#L{f['line']}"} if base else {})}
                     for f in ranked[:TOP_FINDINGS]],
        "findings_shown": min(TOP_FINDINGS, len(ranked)), "findings_total": len(ranked),
        "fix_plan": fix_plan(ranked[:TOP_FINDINGS], base),
        "note": "Read-only static audit. REMOVE means no use was found — not yet proved by tests. Authorship comes "
                "from assistant signatures in commits; code pasted from a chat window has none, so 'no AI trace' "
                "never means 'written by a person'.",
    })
    if m.get("jlr_percent") is None:
        out["note"] = ("No Python here, so there is no dead-code audit; other languages were checked for copied blocks. "
                       + out["note"])
    if hist.get("thin"):
        out["authorship_caveat"] = (f"{hist['largest_commit_percent']}% of all lines arrived in one commit, so the "
                                    "history cannot show how this code was written.")
    return out


def _caller(ctx: Context | None) -> str | None:
    """The account behind this call's token, when accounts are on."""
    try:
        req = ctx.request_context.request if ctx else None
    except ValueError:
        return None
    user = req.scope.get("user") if req is not None else None
    token = getattr(user, "access_token", None)
    return getattr(token, "subject", None)


def build(jobs, accounts, base_url: str, auth_provider=None, auth_settings=None) -> MCPServer:
    server = MCPServer(name="justify", title="Justify", instructions=INSTRUCTIONS,
                       auth_server_provider=auth_provider, auth=auth_settings)
    hops = int(os.environ.get("JUSTIFY_TRUSTED_PROXY_HOPS", "1"))

    async def wait_for(scan_id: str, viewer: str | None) -> dict | None:
        deadline = time.monotonic() + WAIT_S
        scan = await anyio.to_thread.run_sync(lambda: jobs.get(scan_id, viewer=viewer))
        while scan and scan["status"] in ("queued", "cloning", "scanning") and time.monotonic() < deadline:
            await anyio.sleep(1.5)
            scan = await anyio.to_thread.run_sync(lambda: jobs.get(scan_id, viewer=viewer))
        return scan

    def who(ctx):
        user_id = _caller(ctx)
        user = accounts.user(user_id) if user_id else None
        ip = client_ip(ctx.headers if ctx else None, None, hops)
        return user, ip

    @server.tool(title="Audit a public GitHub repository", annotations=_ann())
    async def scan_github_repo(repo: str, ref: str = "", ctx: Context | None = None) -> dict:
        """Audit a public GitHub repository (URL or owner/name; optional branch or tag in ref): unused code and
        duplicates in Python, copied blocks in other languages, a census of languages, and how much came from
        AI-signed commits. Returns findings and a fix plan. Read-only."""
        try:
            rr = parse(repo, ref)
        except FetchError as exc:
            return {"error": str(exc)}
        user, ip = who(ctx)
        try:
            scan, _ = await anyio.to_thread.run_sync(lambda: jobs.submit(
                rr, f"mcp:{ip}", owner=user["id"] if user else None,
                charge=lambda tx: accounts.charge(user, ip, tx, channel="mcp")))
        except QuotaExceeded as exc:
            return {"error": str(exc), "used": exc.used, "limit": exc.limit,
                    "dashboard": f"{base_url}/dashboard" if base_url else "/dashboard"}
        except FetchError as exc:
            return {"error": str(exc)}
        await anyio.to_thread.run_sync(lambda: accounts.remember(user, scan["id"], "mcp"))
        return compact(await wait_for(scan["id"], user["id"] if user else None), base_url)

    @server.tool(title="Audit code the user shares", annotations=_ann())
    async def audit_code(files: list[dict], name: str = "shared-code", ctx: Context | None = None) -> dict:
        """Audit the user's own code that is not on GitHub — files they shared in this chat. Pass every file as
        {"path": "src/app.py", "content": "<the whole file>"} (up to 2,000 files, 8 MB). The result is private to the
        user's account. Same audit as scan_github_repo, without authorship (there is no git history)."""
        user, ip = who(ctx)
        if user is None:
            return {"error": "Auditing your own code needs a Justify account, so the result stays private to you. "
                             "Connect Justify with sign-in, or use a public GitHub repository."}
        try:
            dest = await anyio.to_thread.run_sync(lambda: unpack_files(files, os.path.join(jobs.dir, "uploads")))
            scan = await anyio.to_thread.run_sync(lambda: jobs.submit_upload(
                dest, clean_name(name), user["id"], f"mcp:{ip}",
                charge=lambda tx: accounts.charge(user, ip, tx, channel="mcp")))
        except QuotaExceeded as exc:
            return {"error": str(exc), "used": exc.used, "limit": exc.limit}
        except FetchError as exc:
            return {"error": str(exc)}
        await anyio.to_thread.run_sync(lambda: accounts.remember(user, scan["id"], "mcp-upload"))
        return compact(await wait_for(scan["id"], user["id"]), base_url)

    @server.tool(title="Get a scan's result", annotations=_ann())
    async def get_scan_result(scan_id: str, ctx: Context | None = None) -> dict:
        """The result of a scan started by scan_github_repo or audit_code, by its scan_id. Waits briefly if it is
        still running."""
        if not isinstance(scan_id, str) or not scan_id.startswith("s_") or len(scan_id) > 40:
            return {"error": "That is not a scan_id from scan_github_repo or audit_code."}
        user, _ = who(ctx)
        scan = await wait_for(scan_id, user["id"] if user else None)
        if not scan:
            return {"error": "No scan with that id. Start one with scan_github_repo."}
        return compact(scan, base_url)

    @server.tool(title="The user's recent audits", annotations=_ann())
    async def my_audits(limit: int = 10, ctx: Context | None = None) -> dict:
        """The signed-in user's most recent audits — repository, date, Justified Line Ratio, dead and duplicate
        lines — with the change since the previous audit of the same repository, and today's allowance."""
        user, ip = who(ctx)
        if user is None:
            return {"error": "No account on this connection. Connect Justify with sign-in to keep a history."}
        rows = await anyio.to_thread.run_sync(lambda: accounts.history(user["id"], max(1, min(limit, 50))))
        prev: dict[str, dict] = {}
        older = await anyio.to_thread.run_sync(lambda: accounts.history(user["id"], 200))
        for r in reversed(older):                    # oldest first: each repo's previous audit
            r["_prev"] = prev.get(r["repo"])
            if r.get("summary"):
                prev[r["repo"]] = r["summary"]
        by_id = {r["id"]: r for r in older}
        audits = []
        for r in rows:
            s, p = r.get("summary") or {}, (by_id.get(r["id"]) or {}).get("_prev") or {}
            audits.append({"scan_id": r["id"], "repo": r["repo"], "status": r["status"], "private": r["private"],
                           "justified_line_ratio_percent": s.get("jlr"), "dead_weight_lines": s.get("dead_lines"),
                           "duplicate_lines": s.get("dup_lines"),
                           "change_since_previous": ({"jlr_points": round((s.get("jlr") or 0) - (p.get("jlr") or 0), 2),
                                                      "dead_lines": (s.get("dead_lines") or 0) - (p.get("dead_lines") or 0),
                                                      "duplicate_lines": (s.get("dup_lines") or 0) - (p.get("dup_lines") or 0)}
                                                     if p and s else None),
                           "report": f"{base_url}/s/{r['id']}"})
        return {"user": user.get("login") or user.get("name"), "allowance": accounts.usage(user, ip, "mcp"),
                "audits": audits, "dashboard": f"{base_url}/dashboard"}

    return server
