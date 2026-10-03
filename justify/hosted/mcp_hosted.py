"""
The public MCP endpoint: two tools any AI assistant can call with nothing but a URL.

It scans public GitHub repositories only, and never runs their code or a model. Calls wait
up to WAIT_S for a scan to finish — assistants give a tool a few minutes at most — and
otherwise hand back a scan_id to ask about again. Results are trimmed to stay well under
what assistants accept in one tool result.
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

from .fetch import FetchError, parse
from .ratelimit import client_ip

WAIT_S = 110
TOP_FINDINGS = 60
ORDER = {"REMOVE": 0, "SIMPLIFY": 1, "AMBIGUOUS": 2, "KEEP": 3}

INSTRUCTIONS = (
    "Justify audits a public GitHub repository for code that cannot justify its existence — unused imports, "
    "functions, classes and dependencies, and duplicate helpers — and says how much of it came from AI-assisted "
    "versus human commits. This hosted server reads code only: it never runs the repository or its tests, so "
    "every REMOVE here is a candidate, not a proved removal. To prove removals with the project's own tests, or to "
    "scan private code, the user installs Justify locally (pip install \"justify-code[mcp]\"). Call "
    "scan_github_repo with the repository URL; if it returns a scan_id that is still running, call "
    "get_scan_result with it.")


def _ann():
    return ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                           openWorldHint=True) if ToolAnnotations else None


def compact(scan: dict, base_url: str) -> dict:
    """A scan as a tool result: the numbers, the strongest findings, and a link to the rest."""
    out = {"scan_id": scan["id"], "repo": scan["repo"], "commit": scan.get("sha"), "status": scan["status"],
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
    out.update({
        "files": res.get("files"), "lines": res.get("lines"),
        "justified_line_ratio_percent": m.get("jlr_percent"),
        "dead_weight_lines": m.get("dead_weight_lines"), "dead_weight_per_1000_lines": m.get("per_1000_lines"),
        "duplicate_lines": m.get("duplicate_lines"),
        "counts": counts,
        "authorship": {k: a.get(k) for k in ("ai_lines", "human_lines", "ai_commits", "commits", "ai_dead_per_1000",
                                              "human_dead_per_1000", "ai_dup_per_1000", "human_dup_per_1000",
                                              "ai_to_human_ratio")} if a else None,
        "rework": a.get("rework") if a else None,
        "findings": [{"verdict": f["verdict"], "kind": f["kind"], "file": f["file"], "line": f["line"],
                      "name": f["name"], "reason": (f.get("reason") or "")[:200],
                      "written_by": f.get("authored_by"),
                      "link": f"{res.get('github_blob_base', '')}{f['file']}#L{f['line']}"}
                     for f in ranked[:TOP_FINDINGS]],
        "findings_shown": min(TOP_FINDINGS, len(ranked)), "findings_total": len(ranked),
        "note": "Read-only static audit. REMOVE means no use was found — not yet proved by tests. "
                "AI-assisted = commits with an assistant trailer, so the AI share is a lower bound.",
    })
    return out


def build(jobs, limiter, base_url: str) -> MCPServer:
    server = MCPServer(name="justify", title="Justify", instructions=INSTRUCTIONS)

    async def wait_for(scan_id: str) -> dict:
        deadline = time.monotonic() + WAIT_S
        scan = await anyio.to_thread.run_sync(jobs.get, scan_id)
        while scan and scan["status"] in ("queued", "cloning", "scanning") and time.monotonic() < deadline:
            await anyio.sleep(1.5)
            scan = await anyio.to_thread.run_sync(jobs.get, scan_id)
        return scan

    @server.tool(title="Audit a public GitHub repository", annotations=_ann())
    async def scan_github_repo(repo: str, ref: str = "", ctx: Context | None = None) -> dict:
        """Audit a public GitHub repository (URL or owner/name; optional branch or tag in ref) for code that
        cannot justify its existence, and split it by AI-assisted versus human authorship. Read-only."""
        try:
            rr = parse(repo, ref)
        except FetchError as exc:
            return {"error": str(exc)}
        ip = client_ip(getattr(ctx, "headers", None) if ctx else None, None,
                       int(os.environ.get("JUSTIFY_TRUSTED_PROXY_HOPS", "1")))
        ok, wait = limiter.take(f"mcp:{ip}")
        if not ok:
            return {"error": f"Too many scans from this address. Try again in about {wait // 60 + 1} minutes."}
        try:
            scan, _ = await anyio.to_thread.run_sync(jobs.submit, rr, f"mcp:{ip}")
        except FetchError as exc:
            return {"error": str(exc)}
        return compact(await wait_for(scan["id"]), base_url)

    @server.tool(title="Get a scan's result", annotations=_ann())
    async def get_scan_result(scan_id: str) -> dict:
        """The result of a scan started by scan_github_repo, by its scan_id. Waits briefly if it is still running."""
        if not isinstance(scan_id, str) or not scan_id.startswith("s_") or len(scan_id) > 40:
            return {"error": "That is not a scan_id from scan_github_repo."}
        scan = await wait_for(scan_id)
        if not scan:
            return {"error": "No scan with that id. Start one with scan_github_repo."}
        return compact(scan, base_url)

    return server
