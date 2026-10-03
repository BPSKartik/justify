"""
The hosted service: one process serving the pages, the JSON API, sign-in and the MCP endpoint.

    /, /s/<id>              the audit page, optionally opened on one scan (shareable)
    /dashboard              a signed-in person's audits, statistics, allowance, tokens and apps
    /signin                 choose GitHub or Microsoft
    /api/scans              POST {repo, ref} -> a scan (202 while it runs)
    /api/uploads            POST a .zip, or {name, files:[{path, content}]} -> a private scan
    /api/scans/<id>         the scan, with its result when done (private scans: owner only)
    /api/scans/<id>/report.md
    /api/recent, /api/config, /api/me, /api/me/*
    /auth/<provider>/start, /auth/<provider>/callback, /auth/logout
    /oauth/consent          where an AI app's sign-in request is approved
    /authorize, /token, /register, /revoke, /.well-known/*   OAuth for MCP (served by the MCP SDK)
    /mcp                    streamable HTTP, stateless
    /health
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import pathlib
import threading
import time
from urllib.parse import parse_qs, quote, urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                                 Response)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from .accounts import Accounts, QuotaExceeded
from .db import Database
from .fetch import FetchError, parse
from .jobs import Jobs
from .login import LoginError, LoginFlow, providers_from_env, safe_next
from .mcp_hosted import build
from .ratelimit import RateLimiter, client_ip
from .storage import Results, Snapshots, blob_from_env
from .uploads import clean_name, unpack_files, unpack_zip

STATIC = pathlib.Path(__file__).parent / "static"
SESSION_COOKIE = "jfy_session"
STATE_COOKIE = "jfy_login"

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data: https://avatars.githubusercontent.com; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; worker-src 'self'")


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
                has_csp = any(k.lower() == b"content-security-policy" for k, _ in headers)
                headers += [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                            (b"strict-transport-security", b"max-age=31536000")]
                if not is_mcp:
                    if not has_csp:
                        headers.append((b"content-security-policy", CSP.encode()))
                    headers.append((b"x-frame-options", b"DENY"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


def _consent_page(client: dict, user: dict, req_id: str, csrf: str, redirect_uri: str) -> str:
    name = html.escape((client.get("client_name") or "An AI app")[:80])
    host = html.escape(urlparse(redirect_uri).hostname or urlparse(redirect_uri).scheme or "")
    who = html.escape(user.get("login") or user.get("name") or "you")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect {name} to Justify</title>
<link rel="icon" href="/static/favicon.svg"><link rel="stylesheet" href="/static/styles.css"></head>
<body class="plain"><main class="consent">
<img class="consent-mark" src="/static/favicon.svg" alt="" width="44" height="44">
<h1><strong>{name}</strong> wants to use Justify as <strong>{who}</strong></h1>
<p class="consent-host">It will send you back to <code>{host}</code>.</p>
<ul class="consent-list">
<li>Run audits of public GitHub repositories and of code you share with it, counted against your daily allowance</li>
<li>Read the audits in your history</li>
</ul>
<p class="consent-note">It cannot see your GitHub or Microsoft account, change any code, or act anywhere else.
You can disconnect it at any time from your dashboard.</p>
<form method="post" action="/oauth/consent" class="consent-actions">
<input type="hidden" name="req" value="{html.escape(req_id)}"><input type="hidden" name="csrf" value="{html.escape(csrf)}">
<button class="btn-ghost" name="decision" value="deny" type="submit">Cancel</button>
<button class="btn-primary" name="decision" value="allow" type="submit">Allow</button>
</form></main></body></html>"""


def _message_page(title: str, body: str, status: int = 400) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<link rel="icon" href="/static/favicon.svg"><link rel="stylesheet" href="/static/styles.css"></head>
<body class="plain"><main class="consent"><h1>{html.escape(title)}</h1><p>{html.escape(body)}</p>
<p><a class="btn-ghost" href="/">Back to Justify</a></p></main></body></html>""", status_code=status)


def create_app(jobs: Jobs | None = None) -> Starlette:
    data_dir = os.environ.get("JUSTIFY_DATA_DIR") or "/tmp/justify-hosted"
    base_url = (os.environ.get("JUSTIFY_PUBLIC_URL") or "http://localhost:8000").rstrip("/")
    secure_cookies = base_url.startswith("https://")
    public_hosts = [h.strip() for h in os.environ.get("JUSTIFY_PUBLIC_HOSTS", "").split(",") if h.strip()]
    hops = _env_int("JUSTIFY_TRUSTED_PROXY_HOPS", 1)

    blob = blob_from_env()
    snapshots = None
    if jobs is None:
        db_path = os.path.join(data_dir, "justify.sqlite3")
        if blob:
            snapshots = Snapshots(blob, db_path)
            snapshots.restore()
        database = Database(db_path)
        jobs = Jobs(data_dir, workers=_env_int("JUSTIFY_WORKERS", 2), queue_max=_env_int("JUSTIFY_QUEUE_MAX", 30),
                    scan_timeout=_env_int("JUSTIFY_SCAN_TIMEOUT_S", 600),
                    clone_timeout=_env_int("JUSTIFY_CLONE_TIMEOUT_S", 180),
                    max_mb=_env_int("JUSTIFY_MAX_REPO_MB", 400), scan_mem_mb=_env_int("JUSTIFY_SCAN_MEM_MB", 3072),
                    db=database, results=Results(os.path.join(data_dir, "results"), blob))
    database = jobs.database
    accounts = Accounts(database)
    burst = RateLimiter(_env_int("JUSTIFY_RATE_PER_HOUR", 60))     # any address, signed in or not
    providers = providers_from_env()
    dev_login = os.environ.get("JUSTIFY_DEV_LOGIN") == "1"
    login = LoginFlow(database, providers, base_url)
    accounts_on = bool(providers) or dev_login
    mcp_auth = accounts_on and os.environ.get("JUSTIFY_MCP_AUTH", "required") != "off"

    oauth = settings = None
    if mcp_auth:
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
        from .oauth_server import JustifyOAuth
        oauth = JustifyOAuth(database, accounts, base_url)
        settings = AuthSettings(issuer_url=base_url, resource_server_url=f"{base_url}/mcp",
                                service_documentation_url=f"{base_url}/#connect",
                                client_registration_options=ClientRegistrationOptions(
                                    enabled=True, valid_scopes=["audit"], default_scopes=["audit"]),
                                revocation_options=RevocationOptions(enabled=True), validate_token_resource=False)

    from mcp.server.transport_security import TransportSecuritySettings
    if public_hosts:
        # a public server: callers are vendor clouds (Claude, ChatGPT, Copilot); Origin checks would only
        # break them, and DNS rebinding protects servers on localhost, which this is not
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        local = ["127.0.0.1", "localhost", "[::1]", "testserver"]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                             allowed_hosts=local + [f"{h}:*" for h in local],
                                             allowed_origins=[f"http://{h}" for h in local] + [f"http://{h}:*" for h in local])
    mcp = build(jobs, accounts, base_url, oauth, settings)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                      transport_security=security, max_request_body_size=9_000_000)

    # ---------------------------------------------------------------- helpers

    def err(message: str, status: int, code: str, **extra) -> JSONResponse:
        headers = {"Retry-After": str(extra["retry_after"])} if "retry_after" in extra else None
        return JSONResponse({"error": message, "code": code, **extra}, status_code=status, headers=headers)

    def ip_of(request: Request) -> str:
        return client_ip(request.headers, request.client.host if request.client else None, hops)

    def viewer(request: Request) -> tuple[dict | None, str | None, str]:
        """(user, csrf, how). A bearer token or the session cookie; neither means anonymous."""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return accounts.token_user(auth[7:].strip()), None, "token"
        found = accounts.session(request.cookies.get(SESSION_COOKIE))
        if found:
            return found[0], found[1], "session"
        return None, None, "anonymous"

    def csrf_ok(request: Request, csrf: str | None, how: str) -> bool:
        if how != "session":
            return True
        sent = request.headers.get("x-justify-csrf", "")
        return bool(csrf) and len(sent) == len(csrf) and sent == csrf

    async def who(request: Request, mutate: bool = False):
        user, csrf, how = await run_in_threadpool(viewer, request)
        if mutate and not csrf_ok(request, csrf, how):
            return None, None, how, err("Reload the page and try again.", 403, "csrf")
        return user, csrf, how, None

    def need_user(user) -> JSONResponse | None:
        if user is None:
            return err("Sign in to see this.", 401, "signin")
        return None

    def quota_error(exc: QuotaExceeded, user) -> JSONResponse:
        return err(str(exc), 429, "quota", used=exc.used, limit=exc.limit, signin=user is None,
                   resets_at=accounts.usage(user).get("resets_at"))

    def set_session(resp: Response, value: str) -> None:
        resp.set_cookie(SESSION_COOKIE, value, max_age=30 * 86400, httponly=True, secure=secure_cookies,
                        samesite="lax", path="/")

    # ---------------------------------------------------------------- pages

    def page(name: str):
        async def handler(request: Request) -> Response:
            return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})
        return handler

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "version": __version__, **(await run_in_threadpool(jobs.counts))})

    async def config(request: Request) -> Response:
        q = accounts.quotas
        local = (request.url.hostname or "") in ("localhost", "127.0.0.1", "testserver")
        return JSONResponse({"version": __version__, "accounts": accounts_on, "mcp_auth": mcp_auth,
                             "providers": [{"key": k, "label": p.label} for k, p in providers.items()]
                             + ([{"key": "dev", "label": "Developer (local only)"}] if dev_login and local else []),
                             "quotas": {"anonymous": q.anonymous, "member": q.member},
                             "mcp_url": f"{base_url}/mcp"}, headers={"Cache-Control": "max-age=60"})

    # ---------------------------------------------------------------- scans

    async def create_scan(request: Request) -> Response:
        if int(request.headers.get("content-length") or 0) > 4_000:
            return err("That request is too large.", 413, "too_large")
        user, _, how, bad = await who(request, mutate=True)
        if bad:
            return bad
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
        ip = ip_of(request)
        ok, wait = burst.take(f"web:{ip}")
        if not ok:
            return err(f"That is a lot of requests from one address. Try again in about {wait // 60 + 1} minutes.",
                       429, "rate_limited", retry_after=wait)
        try:
            scan, _ = await run_in_threadpool(jobs.submit, rr, f"web:{ip}", user["id"] if user else None,
                                              lambda tx: accounts.charge(user, ip, tx))
        except QuotaExceeded as exc:
            return quota_error(exc, user)
        except FetchError as exc:
            return err(str(exc), exc.status, exc.code)
        await run_in_threadpool(accounts.remember, user, scan["id"], "web" if how == "session" else "api")
        return JSONResponse(scan, status_code=202 if scan["status"] != "done" else 200)

    async def create_upload(request: Request) -> Response:
        user, _, how, bad = await who(request, mutate=True)
        if bad:
            return bad
        if user is None:
            return err("Sign in to audit your own code — the result stays private to your account.", 401, "signin")
        ip = ip_of(request)
        ok, wait = burst.take(f"web:{ip}")
        if not ok:
            return err("That is a lot of requests from one address. Try again shortly.", 429, "rate_limited",
                       retry_after=wait)
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        limit = 26 * 1024 * 1024 if ctype != "application/json" else 9 * 1024 * 1024
        if int(request.headers.get("content-length") or 0) > limit:
            return err("That upload is too large. Zip it (25 MB at most).", 413, "too_large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return err("That upload is too large. Zip it (25 MB at most).", 413, "too_large")
            chunks.append(chunk)
        data = b"".join(chunks)
        root = os.path.join(jobs.dir, "uploads")
        try:
            if ctype == "application/json":
                body = json.loads(data or b"{}")
                if not isinstance(body, dict):
                    return err("Send {\"name\": ..., \"files\": [...]}.", 400, "bad_json")
                name = clean_name(str(body.get("name") or "code"))
                dest = await run_in_threadpool(unpack_files, body.get("files"), root)
            else:
                name = clean_name(request.query_params.get("name") or "code.zip")
                dest = await run_in_threadpool(unpack_zip, data, root)
            scan = await run_in_threadpool(lambda: jobs.submit_upload(
                dest, name, user["id"], f"web:{ip}", charge=lambda tx: accounts.charge(user, ip, tx)))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return err("Send {\"name\": ..., \"files\": [...]}.", 400, "bad_json")
        except QuotaExceeded as exc:
            return quota_error(exc, user)
        except FetchError as exc:
            return err(str(exc), exc.status, exc.code)
        await run_in_threadpool(accounts.remember, user, scan["id"], "upload")
        return JSONResponse(scan, status_code=202)

    async def get_scan(request: Request) -> Response:
        scan_id = request.path_params["scan_id"]
        if not scan_id.startswith("s_") or len(scan_id) > 40:
            return err("No scan with that id.", 404, "not_found")
        user, _, _, _ = await who(request)
        scan = await run_in_threadpool(lambda: jobs.get(scan_id, viewer=user["id"] if user else None))
        if not scan:
            return err("No scan with that id — or it is private to someone else.", 404, "not_found")
        cache = "no-store" if scan["status"] != "done" or scan["private"] else "max-age=300"
        return JSONResponse(scan, headers={"Cache-Control": cache})

    async def report_md(request: Request) -> Response:
        user, _, _, _ = await who(request)
        scan_id = request.path_params["scan_id"]
        scan = await run_in_threadpool(lambda: jobs.get(scan_id, viewer=user["id"] if user else None))
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
                     metrics=r["metrics"], judging=None, proof=None, languages=r.get("languages") or [])
        return PlainTextResponse(markdown(res), media_type="text/markdown; charset=utf-8",
                                 headers={"Cache-Control": "no-store" if scan["private"] else "max-age=300"})

    async def recent(request: Request) -> Response:
        return JSONResponse({"scans": await run_in_threadpool(jobs.recent, 12)},
                            headers={"Cache-Control": "max-age=15"})

    # ---------------------------------------------------------------- the signed-in person

    async def me(request: Request) -> Response:
        user, csrf, how, _ = await who(request)
        if user is None:
            return err("Not signed in.", 401, "signin")
        usage = await run_in_threadpool(accounts.usage, user, ip_of(request))
        count = await run_in_threadpool(accounts.history_count, user["id"])
        return JSONResponse({"user": user, "csrf": csrf, "usage": usage, "audits": count, "via": how},
                            headers={"Cache-Control": "no-store"})

    async def my_scans(request: Request) -> Response:
        user, _, _, _ = await who(request)
        if (bad := need_user(user)):
            return bad
        limit = max(1, min(int(request.query_params.get("limit") or 50), 200))
        offset = max(0, int(request.query_params.get("offset") or 0))
        rows = await run_in_threadpool(accounts.history, user["id"], limit, offset)
        return JSONResponse({"scans": rows, "total": await run_in_threadpool(accounts.history_count, user["id"])},
                            headers={"Cache-Control": "no-store"})

    async def my_stats(request: Request) -> Response:
        user, _, _, _ = await who(request)
        if (bad := need_user(user)):
            return bad
        rows = await run_in_threadpool(accounts.history, user["id"], 1000)
        return JSONResponse(_stats(rows), headers={"Cache-Control": "no-store"})

    async def my_compare(request: Request) -> Response:
        user, _, _, _ = await who(request)
        if (bad := need_user(user)):
            return bad
        repo = (request.query_params.get("repo") or "")[:200]
        audits = await run_in_threadpool(jobs.audits_of, repo, user["id"])
        diff = None
        if len(audits) >= 2:
            new, old = await run_in_threadpool(lambda: (jobs.results.get(audits[0]["id"]),
                                                        jobs.results.get(audits[1]["id"])))
            if new and old:
                diff = _diff(old, new)
        return JSONResponse({"repo": repo, "audits": audits, "diff": diff}, headers={"Cache-Control": "no-store"})

    async def forget_scan(request: Request) -> Response:
        user, _, _, bad = await who(request, mutate=True)
        if bad or (bad := need_user(user)):
            return bad
        scan_id = request.path_params["scan_id"]
        removed = await run_in_threadpool(accounts.forget, user["id"], scan_id)
        deleted = await run_in_threadpool(jobs.delete_private, scan_id, user["id"])
        return JSONResponse({"ok": removed or deleted, "deleted_result": deleted})

    async def tokens(request: Request) -> Response:
        user, _, how, bad = await who(request, mutate=request.method != "GET")
        if bad or (bad := need_user(user)):
            return bad
        if request.method == "GET":
            return JSONResponse({"tokens": await run_in_threadpool(accounts.personal_tokens, user["id"])},
                                headers={"Cache-Control": "no-store"})
        if how != "session":
            return err("Make tokens from the dashboard.", 403, "session_only")
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        try:
            made = await run_in_threadpool(accounts.new_personal_token, user["id"], str((body or {}).get("name") or ""))
        except ValueError as exc:
            return err(str(exc), 400, "too_many_tokens")
        return JSONResponse(made, status_code=201, headers={"Cache-Control": "no-store"})

    async def revoke_token(request: Request) -> Response:
        user, _, _, bad = await who(request, mutate=True)
        if bad or (bad := need_user(user)):
            return bad
        ok = await run_in_threadpool(accounts.revoke_personal_token, user["id"], request.path_params["token_id"])
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    async def apps(request: Request) -> Response:
        user, _, _, bad = await who(request, mutate=request.method != "GET")
        if bad or (bad := need_user(user)):
            return bad
        if request.method == "DELETE":
            ok = await run_in_threadpool(accounts.disconnect_app, user["id"], request.path_params["client_id"])
            return JSONResponse({"ok": ok}, status_code=200 if ok else 404)
        return JSONResponse({"apps": await run_in_threadpool(accounts.connected_apps, user["id"])},
                            headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- signing in and out

    async def auth_start(request: Request) -> Response:
        key = request.path_params["provider"]
        nxt = safe_next(request.query_params.get("next"))
        if key == "dev":
            if not dev_login or (request.url.hostname or "") not in ("localhost", "127.0.0.1", "testserver"):
                return _message_page("Not available", "Developer sign-in only works on a local machine.", 404)
            name = (request.query_params.get("name") or "developer")[:40]
            user = await run_in_threadpool(accounts.sign_in, "dev", name, {"login": name, "name": name})
            value, _ = await run_in_threadpool(accounts.new_session, user["id"])
            resp = RedirectResponse(nxt, status_code=303)
            set_session(resp, value)
            return resp
        if key not in providers:
            return _message_page("Not available", "That sign-in option is not switched on here.", 404)
        url, state = await run_in_threadpool(login.start, key, nxt)
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=secure_cookies, samesite="lax",
                        path="/auth")
        return resp

    async def auth_callback(request: Request) -> Response:
        key = request.path_params["provider"]
        if key not in providers:
            return _message_page("Not available", "That sign-in option is not switched on here.", 404)
        q = request.query_params
        if q.get("error"):
            return RedirectResponse(f"/signin?error={quote('Sign-in was cancelled.')}", status_code=303)
        try:
            subject, profile, nxt = await run_in_threadpool(login.finish, key, q.get("code", ""), q.get("state", ""),
                                                            request.cookies.get(STATE_COOKIE))
            user = await run_in_threadpool(accounts.sign_in, key, subject, profile)
        except LoginError as exc:
            return RedirectResponse(f"/signin?error={quote(str(exc).capitalize())}", status_code=303)
        value, _ = await run_in_threadpool(accounts.new_session, user["id"])
        print(json.dumps({"event": "sign_in", "provider": key, "user": user["id"]}), flush=True)
        resp = RedirectResponse(nxt, status_code=303)
        set_session(resp, value)
        resp.delete_cookie(STATE_COOKIE, path="/auth")
        return resp

    async def logout(request: Request) -> Response:
        user, csrf, how, bad = await who(request, mutate=True)
        if bad:
            return bad
        await run_in_threadpool(accounts.end_session, request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ---------------------------------------------------------------- an AI app asks to connect

    async def consent(request: Request) -> Response:
        if oauth is None:
            return _message_page("Not available", "Sign-in for AI apps is not switched on here.", 404)
        if request.method == "POST":
            raw = (await request.body())[:4000].decode("utf-8", errors="replace")
            form = {k: v[0] for k, v in parse_qs(raw).items()}
            found = await run_in_threadpool(accounts.session, request.cookies.get(SESSION_COOKIE))
            if not found or form.get("csrf") != found[1]:
                return _message_page("Try again", "This approval did not come from your signed-in session.", 403)
            target = await run_in_threadpool(oauth.decide, form.get("req", ""), found[0]["id"],
                                             form.get("decision") == "allow")
            if not target:
                return _message_page("This request expired", "Go back to your AI app and connect Justify again.", 410)
            return RedirectResponse(target, status_code=303)
        req_id = request.query_params.get("req", "")
        pending = await run_in_threadpool(oauth.pending, req_id)
        if not pending:
            return _message_page("This request expired", "Go back to your AI app and connect Justify again.", 410)
        found = await run_in_threadpool(accounts.session, request.cookies.get(SESSION_COOKIE))
        if not found:
            return RedirectResponse(f"/signin?next={quote('/oauth/consent?req=' + req_id)}", status_code=303)
        req, client = pending
        target = urlparse(req["redirect_uri"])
        allowed = f"{target.scheme}://{target.netloc}" if target.scheme in ("http", "https") else f"{target.scheme}:"
        # the approval form redirects to the app; the browser checks that redirect against form-action
        csp = CSP.replace("form-action 'self'", f"form-action 'self' {allowed}")
        return HTMLResponse(_consent_page(client, found[0], req_id, found[1], req["redirect_uri"]),
                            headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})

    async def resource_metadata_root(request: Request) -> Response:
        """Some clients look for the resource metadata without the /mcp suffix."""
        return JSONResponse({"resource": f"{base_url}/mcp", "authorization_servers": [base_url],
                             "scopes_supported": ["audit"], "bearer_methods_supported": ["header"]})

    # ---------------------------------------------------------------- the app

    @contextlib.asynccontextmanager
    async def lifespan(app):
        if snapshots:
            threading.Thread(target=snapshots.run_forever, args=(database,), daemon=True).start()
        async with mcp.session_manager.run():
            yield
        if snapshots:                       # asked to stop: save the last changes before the disk goes
            await run_in_threadpool(snapshots.save, database, True)

    routes = [
        Route("/", page("index.html")), Route("/s/{scan_id}", page("index.html")),
        Route("/dashboard", page("dashboard.html")), Route("/signin", page("signin.html")),
        Route("/health", health),
        Route("/api/config", config),
        Route("/api/scans", create_scan, methods=["POST"]),
        Route("/api/uploads", create_upload, methods=["POST"]),
        Route("/api/scans/{scan_id}", get_scan),
        Route("/api/scans/{scan_id}/report.md", report_md),
        Route("/api/recent", recent),
        Route("/api/me", me),
        Route("/api/me/scans", my_scans),
        Route("/api/me/scans/{scan_id}", forget_scan, methods=["DELETE"]),
        Route("/api/me/stats", my_stats),
        Route("/api/me/compare", my_compare),
        Route("/api/me/tokens", tokens, methods=["GET", "POST"]),
        Route("/api/me/tokens/{token_id}", revoke_token, methods=["DELETE"]),
        Route("/api/me/apps", apps),
        Route("/api/me/apps/{client_id}", apps, methods=["DELETE"]),
        Route("/auth/logout", logout, methods=["POST"]),
        Route("/auth/{provider}/start", auth_start),
        Route("/auth/{provider}/callback", auth_callback),
        Route("/oauth/consent", consent, methods=["GET", "POST"]),
        Mount("/static", app=StaticFiles(directory=STATIC), name="static"),
    ]
    if mcp_auth:
        routes.append(Route("/.well-known/oauth-protected-resource", resource_metadata_root))
    # last: the MCP app with its own routes (/mcp, and with accounts on the OAuth endpoints) and middleware
    routes.append(Mount("", app=mcp_app))
    app = Starlette(routes=routes, lifespan=lifespan,
                    middleware=[Middleware(SecurityHeaders), Middleware(GZipMiddleware, minimum_size=800)])
    app.state.jobs = jobs
    app.state.accounts = accounts
    return app


# ---------------------------------------------------------------- numbers for the dashboard

def _stats(rows: list[dict]) -> dict:
    """Totals, a 30-day activity strip, languages, assistants and per-repository trends, from a
    person's history (newest first)."""
    import datetime as _dt
    done = [r for r in rows if r["status"] == "done" and r.get("summary")]
    latest: dict[str, dict] = {}
    repos: dict[str, dict] = {}
    for r in reversed(done):                          # oldest first
        s = r["summary"]
        entry = repos.setdefault(r["repo"], {"repo": r["repo"], "kind": r["kind"], "audits": 0, "series": []})
        entry["audits"] += 1
        entry["series"].append({"id": r["id"], "at": r["finished"], "jlr": s.get("jlr"),
                                "dead_lines": s.get("dead_lines"), "dup_lines": s.get("dup_lines"),
                                "ai_share": s.get("ai_share"), "sha": (r.get("sha") or "")[:7]})
        latest[r["repo"]] = {**s, "id": r["id"], "finished": r["finished"], "private": r["private"]}
    for entry in repos.values():
        series = entry["series"]
        entry["latest"] = series[-1]
        entry["previous"] = series[-2] if len(series) > 1 else None
        entry["series"] = series[-20:]
    today = _dt.datetime.now(_dt.timezone.utc).date()
    days = {(today - _dt.timedelta(days=i)).isoformat(): 0 for i in range(29, -1, -1)}
    for r in rows:
        if r.get("asked"):
            d = _dt.datetime.fromtimestamp(r["asked"], _dt.timezone.utc).date().isoformat()
            if d in days:
                days[d] += 1
    langs: dict[str, int] = {}
    tools: dict[str, int] = {}
    ai = human = lines = dead = dup = 0
    jlrs = []
    for s in latest.values():
        for lang in s.get("languages") or []:
            if lang.get("audit", "full") in ("full", "copies"):          # code, not docs or data
                langs[lang["name"]] = langs.get(lang["name"], 0) + lang["lines"]
        for k, v in (s.get("tools") or {}).items():
            tools[k] = tools.get(k, 0) + v
        ai += s.get("ai_lines") or 0
        human += s.get("human_lines") or 0
        lines += s.get("code_lines") or s.get("lines") or 0
        dead += s.get("dead_lines") or 0
        dup += s.get("dup_lines") or 0
        if s.get("jlr") is not None:
            jlrs.append(s["jlr"])
    vias: dict[str, int] = {}
    for r in rows:
        vias[r.get("via") or "web"] = vias.get(r.get("via") or "web", 0) + 1
    return {
        "totals": {"audits": len(rows), "finished": len(done), "repositories": len(latest), "code_lines": lines,
                   "dead_lines": dead, "duplicate_lines": dup,
                   "mean_jlr": round(sum(jlrs) / len(jlrs), 2) if jlrs else None,
                   "ai_share": round(100.0 * ai / (ai + human), 1) if (ai + human) else None},
        "activity": [{"day": d, "audits": n} for d, n in days.items()],
        "languages": sorted(({"name": k, "lines": v} for k, v in langs.items()), key=lambda x: -x["lines"])[:8],
        "assistants": sorted(({"name": k, "commits": v} for k, v in tools.items()), key=lambda x: -x["commits"]),
        "via": vias,
        "repositories": sorted(repos.values(), key=lambda e: e["latest"]["at"] or 0, reverse=True)[:50],
    }


def _diff(old: dict, new: dict) -> dict:
    """What changed between two audits of one repository: findings resolved and findings new.
    Matched by kind, file and name — not line, which moves whenever anything above it does."""
    def key(f):
        return (f["kind"], f["file"], f["name"])

    def brief(f):
        return {"verdict": f["verdict"], "kind": f["kind"], "file": f["file"], "line": f["line"], "name": f["name"],
                "lines": f.get("lines", 1)}

    before = {key(f): f for f in old.get("findings", []) if f["verdict"] in ("REMOVE", "SIMPLIFY")}
    after = {key(f): f for f in new.get("findings", []) if f["verdict"] in ("REMOVE", "SIMPLIFY")}
    resolved = [brief(f) for k, f in before.items() if k not in after]
    added = [brief(f) for k, f in after.items() if k not in before]
    m0, m1 = old.get("metrics", {}), new.get("metrics", {})
    return {"resolved": resolved[:100], "resolved_total": len(resolved),
            "resolved_lines": sum(f.get("lines", 1) for f in before.values() if key(f) not in after),
            "new": added[:100], "new_total": len(added),
            "jlr_before": m0.get("jlr_percent"), "jlr_after": m1.get("jlr_percent"),
            "dead_before": m0.get("dead_weight_lines"), "dead_after": m1.get("dead_weight_lines"),
            "dup_before": m0.get("duplicate_lines"), "dup_after": m1.get("duplicate_lines")}


def main(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    print(json.dumps({"event": "start", "version": __version__, "host": host, "port": port}), flush=True)
    uvicorn.run(create_app(), host=host, port=port, proxy_headers=True, forwarded_allow_ips="*",
                log_level="warning", timeout_keep_alive=30, timeout_graceful_shutdown=20)


if __name__ == "__main__":
    main()
