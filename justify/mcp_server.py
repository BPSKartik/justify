"""
Justify as an MCP server — so any AI assistant can check its own work.

Tools:
    scan_repository(path, judge=False)   what cannot justify itself, and who wrote it
    prove_removals(path)                 remove it in a temporary copy and run the tests
    payoff_report(path)                  the markdown report: JLR, AI vs human dead weight
    history(path)                        how the numbers have moved across runs

Two transports: stdio for assistants on this machine (Copilot in VS Code, Claude
Desktop, Claude Code, Cursor), and streamable HTTP for clients that connect to a URL
(`justify mcp --http`).

The rules that keep it safe:
  * the repository is only ever read; edits happen in a temporary copy;
  * the AI cannot choose what runs: the test command comes from JUSTIFY_TEST_COMMAND,
    which a person sets in the MCP configuration. Model judgement (stages 4-5) is off
    unless a caller asks for it, and uses only the model configured in the environment;
  * only folders under JUSTIFY_ALLOWED_ROOTS can be scanned (over HTTP it defaults to the
    folder the server was started in);
  * over HTTP the server listens on 127.0.0.1 unless told otherwise, checks the Host header
    against DNS rebinding, and requires a bearer token when JUSTIFY_MCP_TOKEN is set.
"""

from __future__ import annotations

import os
import pathlib

from mcp.server.mcpserver import MCPServer

try:
    from mcp.types import ToolAnnotations
except ImportError:
    ToolAnnotations = None

from . import NAME
from .engine import run

server = MCPServer(
    name="justify",
    title=NAME,
    instructions=(
        f"{NAME} finds code that cannot justify its existence — unused imports, functions, classes, "
        "dependencies and duplicate helpers — attributes it to AI-assisted or human commits, and proves "
        "removals by running the project's tests on a temporary copy. Call scan_repository before "
        "proposing a deletion and prove_removals before claiming one is safe. Never delete code the "
        "proof did not pass."
    ),
)


def _ann(**kw):
    return ToolAnnotations(**kw) if ToolAnnotations else None


def _allowed(path: str) -> pathlib.Path:
    """The folder, if it is inside an allowed root. Raises otherwise — the caller is an AI,
    and it should not be able to point the scanner (or the test command) at any folder."""
    target = pathlib.Path(path).expanduser().resolve()
    roots = [pathlib.Path(r).expanduser().resolve()
             for r in os.environ.get("JUSTIFY_ALLOWED_ROOTS", "").split(os.pathsep) if r.strip()]
    if roots and not any(target == r or target.is_relative_to(r) for r in roots):
        raise ValueError(f"{target} is outside the folders this server may scan "
                         f"(JUSTIFY_ALLOWED_ROOTS: {', '.join(map(str, roots))})")
    return target


def _brief(res, full: bool = True) -> dict:
    d = res.as_dict()
    d["findings"] = [
        {k: f[k] for k in ("kind", "file", "line", "name", "verdict", "final", "reason", "proof", "authored_by")}
        for f in d["findings"]
    ] if full else []
    return d


@server.tool(title="Scan a repository for dead weight",
             annotations=_ann(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def scan_repository(path: str, judge: bool = False) -> dict:
    """List code that cannot justify its existence, with a verdict, a reason and who wrote it
    (AI-assisted or human commit). Read-only. Set judge=True to also run the model stages,
    using the model configured in the server's environment."""
    model = None
    if judge:
        from .llm import from_environment
        model = from_environment()
    try:
        target = _allowed(path)
    except ValueError as exc:
        return {"refused": str(exc)}
    return _brief(run(target, model=model))


@server.tool(title="Prove the removals are safe",
             annotations=_ann(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def prove_removals(path: str) -> dict:
    """Remove every REMOVE candidate in a temporary copy, check each edit compiles, and run the
    project's tests there. The original is never written. The test command is the one a person set
    in JUSTIFY_TEST_COMMAND; this tool cannot run anything else."""
    command = os.environ.get("JUSTIFY_TEST_COMMAND", "").strip()
    if not command:
        return {"passed": False, "verdict": "KEEP",
                "reason": "No test command is configured. A person must set JUSTIFY_TEST_COMMAND in this "
                          "server's MCP configuration before anything can be proved."}
    try:
        target = _allowed(path)
    except ValueError as exc:
        return {"refused": str(exc), "verdict": "KEEP"}
    return _brief(run(target, prove_command=command))


@server.tool(title="Payoff report",
             annotations=_ann(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def payoff_report(path: str) -> str:
    """A markdown report: Justified Line Ratio, dead weight, and how much of it came from
    AI-assisted versus human commits."""
    from .report import markdown
    try:
        target = _allowed(path)
    except ValueError as exc:
        return f"Refused: {exc}"
    return markdown(run(target))


@server.tool(title="Ledger history",
             annotations=_ann(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def history(path: str) -> list:
    """Every recorded run for this repository: JLR and dead weight over time."""
    from .ledger import Ledger
    return Ledger().history(str(_allowed(path)))


class _BearerToken:
    """ASGI guard: every HTTP request must carry the token a person configured."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            import hmac
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(auth, b"Bearer " + self.token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"missing or wrong bearer token"})
                return
        await self.app(scope, receive, send)


def http_app(host: str = "127.0.0.1", port: int = 8765):
    from mcp.server.transport_security import TransportSecuritySettings
    os.environ.setdefault("JUSTIFY_ALLOWED_ROOTS", os.getcwd())
    local = {"127.0.0.1", "localhost", "::1"}
    hosts = [f"{h}:{port}" for h in (local | {host})] + list(local | {host})
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                         allowed_origins=[f"http://{h}" for h in hosts])
    app = server.streamable_http_app(transport_security=security, host=host)
    token = os.environ.get("JUSTIFY_MCP_TOKEN", "").strip()
    return _BearerToken(app, token) if token else app


def main(http: bool = False, host: str = "127.0.0.1", port: int = 8765) -> None:
    if not http:
        server.run("stdio")
        return
    import sys

    import uvicorn
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("JUSTIFY_MCP_TOKEN"):
        sys.exit("Refusing to listen beyond this machine without JUSTIFY_MCP_TOKEN set.")
    print(f"Justify MCP over HTTP: http://{host}:{port}/mcp  (scanning allowed under "
          f"{os.environ.get('JUSTIFY_ALLOWED_ROOTS') or os.getcwd()})", file=sys.stderr)
    uvicorn.run(http_app(host, port), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
