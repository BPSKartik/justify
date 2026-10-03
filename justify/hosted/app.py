"""
The hosted service: one process serving the web page, the JSON API and the MCP endpoint.

    /                     the page (paste a repository, watch it audited)
    /s/<id>               the same page, opened on one scan — shareable
    /api/scans            POST {repo, ref} -> a scan (202 while it runs)
    /api/scans/<id>       the scan, with its result when done
    /api/scans/<id>/report.md
    /api/recent           the latest scans, one per repository
    /health
    /mcp                  streamable HTTP, stateless, read-only tools
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from .fetch import FetchError, parse
from .jobs import Jobs
from .mcp_hosted import build
from .ratelimit import RateLimiter, client_ip

STATIC = pathlib.Path(__file__).parent / "static"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        is_mcp = scope["path"].startswith("/mcp")

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers += [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                            (b"strict-transport-security", b"max-age=31536000")]
                if not is_mcp:
                    headers += [(b"content-security-policy", CSP.encode()), (b"x-frame-options", b"DENY")]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


def create_app(jobs: Jobs | None = None) -> Starlette:
    data_dir = os.environ.get("JUSTIFY_DATA_DIR") or "/tmp/justify-hosted"
    jobs = jobs or Jobs(data_dir, workers=_env_int("JUSTIFY_WORKERS", 2), queue_max=_env_int("JUSTIFY_QUEUE_MAX", 30),
                        scan_timeout=_env_int("JUSTIFY_SCAN_TIMEOUT_S", 600),
                        clone_timeout=_env_int("JUSTIFY_CLONE_TIMEOUT_S", 180),
                        max_mb=_env_int("JUSTIFY_MAX_REPO_MB", 400), scan_mem_mb=_env_int("JUSTIFY_SCAN_MEM_MB", 3072))
    limiter = RateLimiter(_env_int("JUSTIFY_RATE_PER_HOUR", 20))
    hops = _env_int("JUSTIFY_TRUSTED_PROXY_HOPS", 1)
    base_url = (os.environ.get("JUSTIFY_PUBLIC_URL") or "").rstrip("/")
    public_hosts = [h.strip() for h in os.environ.get("JUSTIFY_PUBLIC_HOSTS", "").split(",") if h.strip()]

    from mcp.server.transport_security import TransportSecuritySettings
    if public_hosts:
        # a public server: callers are vendor clouds (Claude, ChatGPT, Copilot); Origin checks would only
        # break them, and DNS rebinding protects servers on localhost, which this is not
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        local = ["127.0.0.1", "localhost", "[::1]"]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                             allowed_hosts=local + [f"{h}:*" for h in local],
                                             allowed_origins=[f"http://{h}" for h in local] + [f"http://{h}:*" for h in local])
    mcp = build(jobs, limiter, base_url)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                      transport_security=security, max_request_body_size=64_000)

    def err(message: str, status: int, code: str, **extra) -> JSONResponse:
        headers = {"Retry-After": str(extra["retry_after"])} if "retry_after" in extra else None
        return JSONResponse({"error": message, "code": code, **extra}, status_code=status, headers=headers)

    async def index(request: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__, **(await run_in_threadpool(jobs.counts))})

    async def create_scan(request: Request) -> Response:
        if int(request.headers.get("content-length") or 0) > 4_000:
            return err("That request is too large.", 413, "too_large")
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return err("Send JSON: {\"repo\": \"https://github.com/owner/name\"}.", 400, "bad_json")
        if not isinstance(body, dict):
            return err("Send JSON: {\"repo\": \"https://github.com/owner/name\"}.", 400, "bad_json")
        try:
            rr = parse(str(body.get("repo") or ""), str(body.get("ref") or ""))
        except FetchError as exc:
            return err(str(exc), exc.status, exc.code)
        ip = client_ip(request.headers, request.client.host if request.client else None, hops)
        ok, wait = limiter.take(f"web:{ip}")
        if not ok:
            return err(f"That is a lot of scans from one address. Try again in about {wait // 60 + 1} minutes.",
                       429, "rate_limited", retry_after=wait)
        try:
            scan, new = await run_in_threadpool(jobs.submit, rr, f"web:{ip}")
        except FetchError as exc:
            return err(str(exc), exc.status, exc.code)
        return JSONResponse(scan, status_code=202 if scan["status"] != "done" else 200)

    async def get_scan(request: Request) -> Response:
        scan_id = request.path_params["scan_id"]
        if not scan_id.startswith("s_") or len(scan_id) > 40:
            return err("No scan with that id.", 404, "not_found")
        scan = await run_in_threadpool(jobs.get, scan_id)
        if not scan:
            return err("No scan with that id. It may be from before the service restarted.", 404, "not_found")
        return JSONResponse(scan, headers={"Cache-Control": "no-store" if scan["status"] != "done" else "max-age=300"})

    async def report_md(request: Request) -> Response:
        scan = await run_in_threadpool(jobs.get, request.path_params["scan_id"])
        if not scan or scan["status"] != "done":
            return err("That scan has no report yet.", 404, "not_ready")
        from ..engine import Result
        from ..model import Finding
        from ..report import markdown
        r = scan["result"]
        res = Result(root=r["root"], started=r.get("started", ""), files=r["files"], lines=r["lines"],
                     unparsed=r.get("unparsed", []),
                     findings=[Finding(**{k: v for k, v in f.items() if k in Finding.__dataclass_fields__})
                               for f in r["findings"]],
                     metrics=r["metrics"], judging=None, proof=None)
        return PlainTextResponse(markdown(res), media_type="text/markdown; charset=utf-8")

    async def recent(request: Request) -> Response:
        return JSONResponse({"scans": await run_in_threadpool(jobs.recent, 12)},
                            headers={"Cache-Control": "max-age=15"})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    routes = [
        Route("/", index), Route("/s/{scan_id}", index),
        Route("/health", health),
        Route("/api/scans", create_scan, methods=["POST"]),
        Route("/api/scans/{scan_id}", get_scan),
        Route("/api/scans/{scan_id}/report.md", report_md),
        Route("/api/recent", recent),
        *mcp_app.routes,
        Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan,
                    middleware=[Middleware(SecurityHeaders), Middleware(GZipMiddleware, minimum_size=800)])
    app.state.jobs = jobs
    return app


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    print(json.dumps({"event": "start", "version": __version__, "host": host, "port": port}), flush=True)
    uvicorn.run(create_app(), host=host, port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_level="warning", timeout_keep_alive=30)


if __name__ == "__main__":
    main()
