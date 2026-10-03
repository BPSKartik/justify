"""Accounts on the hosted service: signing in, allowances, private uploads, history, personal
tokens, and the OAuth flow an AI app uses to call MCP as a person. Sign-in here is the
developer sign-in, which only works on localhost; the provider round trip is the same code."""

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import time
import zipfile

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from justify.hosted.fetch import FetchError  # noqa: E402
from justify.hosted.jobs import Jobs  # noqa: E402
from justify.hosted.seal import key_text, new_key, unseal  # noqa: E402
from justify.hosted.uploads import unpack_files, unpack_zip  # noqa: E402

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV)


@pytest.fixture
def origin(tmp_path):
    src = tmp_path / "origin"
    src.mkdir()
    (src / "app.py").write_text("import io\nimport json\nprint(json)\n")
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "start\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    return src


@pytest.fixture
def app_client(tmp_path, origin, monkeypatch):
    for k, v in {"JUSTIFY_DEV_LOGIN": "1", "JUSTIFY_PUBLIC_URL": "http://localhost", "JUSTIFY_ANON_DAILY": "1",
                 "JUSTIFY_USER_DAILY": "4", "JUSTIFY_RATE_PER_HOUR": "500"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("JUSTIFY_PUBLIC_HOSTS", raising=False)
    from justify.hosted.app import create_app
    jobs = Jobs(str(tmp_path / "data"), workers=2)

    def fake_clone(rr, branch, dest, timeout, max_mb):
        subprocess.run(["git", "clone", "-q", str(origin), dest], check=True)
        return None                                   # keep the commit the resolver named

    jobs.clone_fn = fake_clone
    jobs.resolve_fn = lambda rr: (hashlib.sha1(rr.slug.lower().encode()).hexdigest(), "main")   # one commit per repo
    with TestClient(create_app(jobs), base_url="http://localhost", follow_redirects=False) as c:
        yield c


def _sign_in(c, name="alice"):
    r = c.get(f"/auth/dev/start?name={name}&next=/dashboard")
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    me = c.get("/api/me").json()
    return me["csrf"], me


def _wait(c, scan_id, headers=None):
    for _ in range(300):
        s = c.get(f"/api/scans/{scan_id}", headers=headers or {}).json()
        if s.get("status") in ("done", "failed"):
            return s
        time.sleep(0.1)
    raise AssertionError("scan did not finish")


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


# ---------------------------------------------------------------- anonymous, then signed in

def test_allowances_privacy_history_and_tokens(app_client):
    c = app_client
    cfg = c.get("/api/config").json()
    assert cfg["accounts"] and cfg["mcp_auth"] and cfg["providers"][0]["key"] == "dev"
    assert c.get("/api/me").status_code == 401

    # anonymous: one new audit a day; the same commit again costs nothing
    r = c.post("/api/scans", json={"repo": "owner/one"})
    assert r.status_code == 202
    first = _wait(c, r.json()["id"])
    assert first["status"] == "done" and first["summary"]["traced"]
    assert c.post("/api/scans", json={"repo": "owner/one"}).status_code == 200
    r = c.post("/api/scans", json={"repo": "owner/two"})
    assert r.status_code == 429 and r.json()["code"] == "quota" and r.json()["signin"] is True

    csrf, me = _sign_in(c)
    assert me["user"]["login"] == "alice" and me["usage"]["limit"] == 4
    assert c.post("/api/scans", json={"repo": "owner/two"}).status_code == 403          # no CSRF header
    h = {"x-justify-csrf": csrf}
    r = c.post("/api/scans", json={"repo": "owner/two"}, headers=h)
    assert r.status_code == 202
    assert _wait(c, r.json()["id"])["status"] == "done"

    # her own code: private to her, sealed with a key only her browser holds
    zipped = _zip({"my-project/lib/a.py": "import os\n\ndef used():\n    return 1\n\nused()\n",
                   "my-project/lib/b.ts": "export const x = 1;\n"})
    nokey = c.post("/api/uploads?name=my-project.zip", headers={**h, "content-type": "application/zip"}, content=zipped)
    assert nokey.status_code == 400 and nokey.json()["code"] == "no_key"
    key = new_key()
    up = c.post("/api/uploads?name=my-project.zip", content=zipped,
                headers={**h, "content-type": "application/zip", "x-justify-result-key": key_text(key)})
    assert up.status_code == 202, up.text
    up_id = up.json()["id"]
    done = _wait(c, up_id)
    assert done["status"] == "done" and done["private"] and done["sealed"] and done["repo"] == "upload/my-project"
    assert done["result"] is None                                  # the server cannot read it back
    sealed = c.get(f"/api/scans/{up_id}/sealed")
    assert sealed.status_code == 200 and sealed.content.startswith(b"JSEAL1")
    assert b"my-project" not in sealed.content and b"lib/a.py" not in sealed.content
    result = unseal(key, up_id, sealed.content)                     # what her browser does
    assert {l["name"] for l in result["languages"]} == {"Python", "TypeScript"}
    assert any(f["name"] == "os" for f in result["findings"])
    with pytest.raises(Exception):
        unseal(new_key(), up_id, sealed.content)
    on_disk = b"".join(p.read_bytes() for p in (c.app.state.jobs.results.dir and
                                                 __import__("pathlib").Path(c.app.state.jobs.results.dir)).iterdir())
    assert b"lib/a.py" not in on_disk and b"used" not in on_disk
    assert c.get(f"/api/scans/{up_id}/report.md").status_code == 409
    stranger = TestClient(c.app, base_url="http://localhost")
    assert stranger.get(f"/api/scans/{up_id}/sealed").status_code == 404
    assert stranger.get(f"/api/scans/{up_id}").status_code == 404
    assert stranger.get(f"/api/scans/{up_id}/report.md").status_code == 404
    assert up_id not in json.dumps(c.get("/api/recent").json())

    hist = c.get("/api/me/scans").json()
    assert hist["total"] == 2 and {s["repo"] for s in hist["scans"]} == {"owner/two", "upload/my-project"}
    stats = c.get("/api/me/stats").json()
    assert stats["totals"]["repositories"] == 2 and len(stats["activity"]) == 30
    assert stats["assistants"] == [{"name": "Claude", "commits": 1}]

    # a personal token works where a header is easier than a browser, until it is revoked
    assert c.post("/api/me/tokens", json={"name": "ci"}).status_code == 403              # no CSRF header
    made = c.post("/api/me/tokens", json={"name": "ci"}, headers=h).json()
    assert made["token"].startswith("jst_")
    bearer = {"authorization": f"Bearer {made['token']}"}
    assert stranger.get("/api/me", headers=bearer).json()["user"]["login"] == "alice"
    assert stranger.get(f"/api/scans/{up_id}", headers=bearer).status_code == 200
    assert c.delete(f"/api/me/tokens/{made['id']}", headers=h).json()["ok"]
    assert stranger.get("/api/me", headers=bearer).status_code == 401

    # forgetting a private audit deletes its result
    assert c.delete(f"/api/me/scans/{up_id}", headers=h).json()["deleted_result"] is True
    assert c.get(f"/api/scans/{up_id}").status_code == 404

    # her allowance: 4 a day, counted only for new audits
    for name in ("three", "four"):
        assert c.post("/api/scans", json={"repo": f"owner/{name}"}, headers=h).status_code == 202
    r = c.post("/api/scans", json={"repo": "owner/five"}, headers=h)
    assert r.status_code == 429 and r.json()["code"] == "quota" and r.json()["signin"] is False
    assert c.post("/auth/logout", headers=h).json()["ok"] and c.get("/api/me").status_code == 401


def test_comparison_shows_what_was_fixed(app_client, origin):
    c = app_client
    csrf, _ = _sign_in(c, "bob")
    h = {"x-justify-csrf": csrf}
    jobs = c.app.state.jobs
    jobs.resolve_fn = lambda rr: ("a" * 40, "main")
    first = _wait(c, c.post("/api/scans", json={"repo": "owner/fixme"}, headers=h).json()["id"])
    (origin / "app.py").write_text("import json\nprint(json)\n")              # the dead import is gone
    _git(origin, "commit", "-qam", "drop io")
    jobs.resolve_fn = lambda rr: ("b" * 40, "main")
    second = _wait(c, c.post("/api/scans", json={"repo": "owner/fixme"}, headers=h).json()["id"])
    assert first["id"] != second["id"]
    cmp = c.get("/api/me/compare?repo=owner/fixme").json()
    assert [a["id"] for a in cmp["audits"]] == [second["id"], first["id"]]
    assert [f["name"] for f in cmp["diff"]["resolved"]] == ["io"] and cmp["diff"]["new_total"] == 0


# ---------------------------------------------------------------- uploads are hostile until proven otherwise

def test_zip_unpacking_refuses_escapes_links_and_git_config(tmp_path):
    root = tmp_path / "up"
    root.mkdir()
    with pytest.raises(FetchError):
        unpack_zip(_zip({"../evil.py": "x"}), str(root))
    with pytest.raises(FetchError):
        unpack_zip(b"not a zip", str(root))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        link = zipfile.ZipInfo("proj/link.py")
        link.external_attr = (0o120777 << 16)                 # a symlink entry
        z.writestr(link, "/etc/passwd")
        z.writestr("proj/ok.py", "x = 1\n")
        z.writestr("proj/.git/HEAD", "ref: refs/heads/main\n")
        z.writestr("proj/.git/config", "[core]\n\tfsmonitor = touch /tmp/pwned\n")
        z.writestr("proj/.git/hooks/post-checkout", "#!/bin/sh\ntouch /tmp/pwned\n")
        z.writestr("proj/.git/objects/info/alternates", "/somewhere/else\n")
    dest = unpack_zip(buf.getvalue(), str(root))
    names = sorted(str(p.relative_to(dest)) for p in __import__("pathlib").Path(dest).rglob("*") if p.is_file())
    assert names == [".git/HEAD", ".git/config", "ok.py"]
    assert "fsmonitor" not in open(os.path.join(dest, ".git", "config")).read()
    with pytest.raises(FetchError):
        unpack_files([{"path": "/etc/passwd", "content": "x"}], str(root))
    with pytest.raises(FetchError):
        unpack_files([{"path": "a/.git/config", "content": "x"}], str(root))


# ---------------------------------------------------------------- an AI app connects with OAuth

def _pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


MCP_HDR = {"accept": "application/json, text/event-stream", "content-type": "application/json",
           "mcp-protocol-version": "2025-06-18"}


def _rpc(c, method, params, token=None, i=1):
    h = {**MCP_HDR, **({"authorization": f"Bearer {token}"} if token else {})}
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": i, "method": method, "params": params}, headers=h)


def test_oauth_for_mcp_end_to_end(app_client):
    c = app_client
    prm = c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["authorization_servers"] == ["http://localhost/"] or prm["authorization_servers"] == ["http://localhost"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/register") and "S256" in meta["code_challenge_methods_supported"]

    r = _rpc(c, "tools/list", {})
    assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")

    bad = c.post("/register", json={"redirect_uris": ["javascript:alert(1)"], "token_endpoint_auth_method": "none",
                                    "grant_types": ["authorization_code"], "response_types": ["code"]})
    assert bad.status_code == 400
    reg = c.post("/register", json={"redirect_uris": ["http://127.0.0.1:9999/cb"], "client_name": "Test AI",
                                    "token_endpoint_auth_method": "none",
                                    "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]

    verifier, challenge = _pkce()
    r = c.get("/authorize", params={"response_type": "code", "client_id": client_id,
                                    "redirect_uri": "http://127.0.0.1:9999/cb", "code_challenge": challenge,
                                    "code_challenge_method": "S256", "state": "xyz", "scope": "audit"})
    assert r.status_code == 302 and "/oauth/consent?req=" in r.headers["location"]
    consent_url = r.headers["location"].replace("http://localhost", "")
    r = c.get(consent_url)
    assert r.status_code == 303 and r.headers["location"].startswith("/signin?next=")      # sign in first

    _sign_in(c, "carol")
    page = c.get(consent_url)
    assert page.status_code == 200 and "Test AI" in page.text and "carol" in page.text
    assert "http://127.0.0.1:9999" in page.headers["content-security-policy"]
    req_id = re.search(r'name="req" value="([^"]+)"', page.text).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    forged = c.post("/oauth/consent", content=f"req={req_id}&csrf=wrong&decision=allow",
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert forged.status_code == 403
    r = c.post("/oauth/consent", content=f"req={req_id}&csrf={csrf}&decision=allow",
               headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("http://127.0.0.1:9999/cb?") and "state=xyz" in loc
    code = re.search(r"code=([^&]+)", loc).group(1)

    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": client_id, "code_verifier": verifier}
    tok = c.post("/token", data=form).json()
    assert tok["access_token"].startswith("jat_") and tok["refresh_token"].startswith("jrt_")
    assert c.post("/token", data=form).status_code == 400                    # a code works once

    init = _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "1"}}, tok["access_token"])
    assert init.status_code == 200
    called = _rpc(c, "tools/call", {"name": "scan_github_repo", "arguments": {"repo": "owner/viamcp"}},
                  tok["access_token"], 2).json()
    body = json.loads(called["result"]["content"][0]["text"])
    assert body["status"] == "done" and body["authorship"]["assistants_seen"] == {"Claude": 1}
    assert body["fix_plan"] and body["fix_plan"][0]["file"] == "app.py"
    mine = json.loads(_rpc(c, "tools/call", {"name": "my_audits", "arguments": {}}, tok["access_token"], 3)
                      .json()["result"]["content"][0]["text"])
    assert mine["user"] == "carol" and mine["audits"][0]["repo"] == "owner/viamcp"

    new = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                 "client_id": client_id}).json()
    assert new["access_token"] != tok["access_token"]
    again = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                   "client_id": client_id})
    assert again.status_code == 400                                          # refresh tokens rotate

    apps = c.get("/api/me/apps").json()["apps"]
    assert apps[0]["name"] == "Test AI" and apps[0]["redirect_host"] == "127.0.0.1"
    h = {"x-justify-csrf": c.get("/api/me").json()["csrf"]}
    assert c.delete(f"/api/me/apps/{client_id}", headers=h).json()["ok"]
    assert _rpc(c, "tools/list", {}, new["access_token"]).status_code == 401


def test_deleting_an_account_erases_it(app_client):
    c = app_client
    csrf, _ = _sign_in(c, "dora")
    h = {"x-justify-csrf": csrf}
    up = c.post("/api/uploads", json={"name": "mine", "files": [{"path": "a.py", "content": "import os\n"}]},
                headers={**h, "x-justify-result-key": key_text(new_key())})
    up_id = up.json()["id"]
    assert _wait(c, up_id)["status"] == "done"
    made = c.post("/api/me/tokens", json={"name": "x"}, headers=h).json()
    assert c.delete("/api/me").status_code == 403                               # no CSRF header
    r = c.delete("/api/me", headers=h)
    assert r.status_code == 200 and r.json()["deleted_private_audits"] == 1
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/me", headers={"authorization": f"Bearer {made['token']}"}).status_code == 401
    db = c.app.state.jobs.database
    assert db.one("SELECT COUNT(*) AS n FROM users")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM scans WHERE id=?", (up_id,))["n"] == 0
    assert c.app.state.jobs.results.get(up_id) is None
    assert c.get("/privacy").status_code == 200


def test_the_service_keeps_no_addresses_no_emails_and_no_readable_private_results(app_client):
    c = app_client
    c.post("/api/scans", json={"repo": "owner/anon"})
    db = c.app.state.jobs.database
    who = [r["who"] for r in db.all("SELECT who FROM usage")]
    assert who and all(w.startswith(("ip:", "u:")) and "." not in w and w.count(":") == 1 for w in who)
    assert all(r["client"] in ("web", "api", "mcp") for r in db.all("SELECT client FROM scans"))
    accounts = c.app.state.accounts
    u = accounts.sign_in("microsoft", "t:o", {"login": "someone@contoso.com", "name": "Some One",
                                               "email": "someone@contoso.com"})
    row = db.one("SELECT * FROM users WHERE id=?", (u["id"],))
    assert row["email"] is None and row["login"] is None and row["name"] == "Some One"
    g = accounts.sign_in("github", "42", {"login": "octo", "name": "Octo", "email": "octo@example.com"})
    assert db.one("SELECT email, login FROM users WHERE id=?", (g["id"],))["login"] == "octo"
    assert db.one("SELECT email FROM users WHERE id=?", (g["id"],))["email"] is None


def test_code_shared_in_a_chat_is_sealed_with_a_key_the_ai_app_holds(app_client):
    c = app_client
    csrf, _ = _sign_in(c, "erin")
    pat = c.post("/api/me/tokens", json={"name": "chat"}, headers={"x-justify-csrf": csrf}).json()["token"]
    _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}, pat)
    files = [{"path": "app.py", "content": "import os\nimport json\nprint(json.dumps({}))\n"}]
    out = json.loads(_rpc(c, "tools/call", {"name": "audit_code", "arguments": {"files": files, "name": "chat-code"}},
                          pat, 2).json()["result"]["content"][0]["text"])
    assert out["status"] == "done" and any(f["name"] == "os" for f in out["findings"]) and out["result_key"]
    again = json.loads(_rpc(c, "tools/call", {"name": "get_scan_result", "arguments": {"scan_id": out["scan_id"]}},
                            pat, 3).json()["result"]["content"][0]["text"])
    assert again["sealed"] and "result_key" in again["error"]                 # no key, no result
    opened = json.loads(_rpc(c, "tools/call", {"name": "get_scan_result",
                                               "arguments": {"scan_id": out["scan_id"], "result_key": out["result_key"]}},
                             pat, 4).json()["result"]["content"][0]["text"])
    assert opened["findings"] == out["findings"]


# ---------------------------------------------------------------- private repositories through the GitHub App

class _FakeApp:
    """Stands in for GitHub: installation 99 sits on GitHub account 4242 and holds one private repository."""

    def install_url(self, state):
        return f"https://github.com/apps/justify-test/installations/new?state={state}"

    def installation(self, installation_id):
        return {"id": installation_id, "account": {"id": 4242, "login": "octo"}}

    def token(self, installation_id):
        return "ghs_installation_token"

    def repos(self, installation_id):
        return [{"full_name": "octo/secret", "name": "secret", "private": True, "description": "", "language": "Python",
                 "stars": 0, "pushed_at": None, "fork": False, "archived": False, "installation_id": installation_id}]


def test_private_repositories_need_the_app_on_your_own_account_and_are_sealed(tmp_path, origin, monkeypatch):
    for k, v in {"JUSTIFY_DEV_LOGIN": "1", "JUSTIFY_PUBLIC_URL": "http://localhost", "JUSTIFY_RATE_PER_HOUR": "500"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("JUSTIFY_PUBLIC_HOSTS", raising=False)
    from justify.hosted.app import create_app
    jobs = Jobs(str(tmp_path / "data"), workers=1)
    seen = {}

    def fake_clone(rr, branch, dest, timeout, max_mb, token=None):
        seen["token"] = token
        subprocess.run(["git", "clone", "-q", str(origin), dest], check=True)

    jobs.clone_fn = fake_clone
    jobs.resolve_fn = lambda rr, token=None: ("c" * 40, "main")
    app = create_app(jobs, ghapp=_FakeApp())
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as c:
        mallory = TestClient(app, base_url="http://localhost", follow_redirects=False)
        accounts = app.state.accounts
        octo = accounts.sign_in("github", "4242", {"login": "octo", "name": "Octo"})
        c.cookies.set("jfy_session", accounts.new_session(octo["id"])[0])
        csrf = c.get("/api/me").json()["csrf"]
        assert c.get("/api/config").json()["github_app"] is True

        # someone else cannot claim octo's installation by typing its id
        eve = accounts.sign_in("github", "777", {"login": "eve", "name": "Eve"})
        mallory.cookies.set("jfy_session", accounts.new_session(eve["id"])[0])
        start = mallory.get("/github/connect")
        state = re.search(r"state=([^&]+)", start.headers["location"]).group(1)
        assert mallory.get(f"/github/installed?installation_id=99&setup_action=install&state={state}").status_code == 403
        assert mallory.get("/api/me/private-repos").json()["repos"] == []

        start = c.get("/github/connect")
        assert start.status_code == 302 and start.headers["location"].startswith("https://github.com/apps/")
        state = re.search(r"state=([^&]+)", start.headers["location"]).group(1)
        assert c.get(f"/github/installed?installation_id=99&setup_action=install&state=forged").status_code == 403
        back = c.get(f"/github/installed?installation_id=99&setup_action=install&state={state}")
        assert back.status_code == 303 and back.headers["location"] == "/?repos=1"
        listed = c.get("/api/me/private-repos").json()
        assert [r["full_name"] for r in listed["repos"]] == ["octo/secret"] and listed["connected"]

        h = {"x-justify-csrf": csrf}
        assert c.post("/api/scans", json={"repo": "octo/other", "private": True},
                      headers={**h, "x-justify-result-key": key_text(new_key())}).status_code == 403
        key = new_key()
        r = c.post("/api/scans", json={"repo": "octo/secret", "private": True}, headers={**h, "x-justify-result-key": key_text(key)})
        assert r.status_code == 202, r.text
        done = _wait(c, r.json()["id"])
        assert done["status"] == "done" and done["sealed"] and done["result"] is None and done["kind"] == "github-private"
        assert seen["token"] == "ghs_installation_token"
        result = unseal(key, done["id"], c.get(f"/api/scans/{done['id']}/sealed").content)
        assert result["github_blob_base"].startswith("https://github.com/octo/secret/blob/")
        assert mallory.get(f"/api/scans/{done['id']}").status_code == 404
        assert done["id"] not in json.dumps(c.get("/api/recent").json())


class _FakeProvider:
    """A sign-in provider that answers from a table: code -> (subject, profile)."""

    def __init__(self, key, label, people):
        self.key, self.label, self.people = key, label, people

    def authorize_url(self, redirect_uri, state, challenge, nonce):
        return f"https://{self.key}.example/authorize?state={state}"

    def profile(self, code, redirect_uri, verifier, nonce):
        return self.people[code]


def _round_trip(c, provider, code, link=False):
    start = c.get(f"/auth/{provider}/start" + ("?link=1" if link else "?next=/dashboard"))
    if start.status_code != 302:
        return start
    state = re.search(r"state=([^&]+)", start.headers["location"]).group(1)
    return c.get(f"/auth/{provider}/callback?code={code}&state={state}")


def test_one_account_can_be_opened_with_github_and_microsoft(tmp_path, monkeypatch):
    for k, v in {"JUSTIFY_PUBLIC_URL": "http://localhost", "JUSTIFY_RATE_PER_HOUR": "500"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("JUSTIFY_PUBLIC_HOSTS", raising=False)
    import justify.hosted.app as app_mod
    gh = _FakeProvider("github", "GitHub", {"g-kartik": ("101", {"login": "kartik", "name": "Kartik"}),
                                           "g-busy": ("202", {"login": "busy", "name": "Busy"})})
    ms = _FakeProvider("microsoft", "Microsoft", {"m-kartik": ("tid:oid", {"login": "k@uni.edu", "name": "Kartik"})})
    monkeypatch.setattr(app_mod, "providers_from_env", lambda: {"github": gh, "microsoft": ms})
    app = app_mod.create_app(Jobs(str(tmp_path / "data"), workers=1))
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as c:
        accounts = app.state.accounts
        # signing in with GitHub first made an account of its own, with nothing in it yet
        first = TestClient(app, base_url="http://localhost", follow_redirects=False)
        assert _round_trip(first, "github", "g-kartik").status_code == 303
        empty_id = first.get("/api/me").json()["user"]["id"]

        # connecting needs a signed-in browser
        assert _round_trip(c, "github", "g-kartik", link=True).headers["location"].startswith("/signin")

        assert _round_trip(c, "microsoft", "m-kartik").status_code == 303
        me = c.get("/api/me").json()
        assert me["providers"] == ["microsoft"] and me["user"]["login"] is None
        back = _round_trip(c, "github", "g-kartik", link=True)
        assert back.status_code == 303 and "linked=" in back.headers["location"]
        me = c.get("/api/me").json()
        assert sorted(me["providers"]) == ["github", "microsoft"] and me["user"]["login"] == "kartik"
        assert accounts.user(empty_id) is None                            # the empty one was folded in
        assert first.get("/api/me").status_code == 401                    # and its session went with it
        # either way in now opens the same account
        again = TestClient(app, base_url="http://localhost", follow_redirects=False)
        _round_trip(again, "github", "g-kartik")
        assert again.get("/api/me").json()["user"]["id"] == me["user"]["id"]
        from urllib.parse import unquote
        assert "already connected" in unquote(_round_trip(c, "github", "g-kartik", link=True).headers["location"])

        # an account with history is never merged silently
        busy = accounts.sign_in("github", "202", {"login": "busy", "name": "Busy"})
        accounts.db.write("INSERT INTO user_scans (user_id, scan_id, created, via) VALUES (?,?,?,?)",
                          (busy["id"], "s_x", time.time(), "web"))
        other = TestClient(app, base_url="http://localhost", follow_redirects=False)
        other.cookies.set("jfy_session", accounts.new_session(accounts.sign_in("dev", "zed", {"login": "zed"})["id"])[0])
        out = _round_trip(other, "github", "g-busy", link=True)
        assert "link_error=" in out.headers["location"]
        assert accounts.identities(busy["id"]) == ["github"]

        # a link started by one account cannot be finished by another signed in meanwhile
        start = c.get("/auth/github/start?link=1")
        state = re.search(r"state=([^&]+)", start.headers["location"]).group(1)
        c.cookies.set("jfy_session", accounts.new_session(busy["id"])[0])
        stolen = c.get(f"/auth/github/callback?code=g-kartik&state={state}")
        assert stolen.headers["location"].startswith("/signin?error=")
        assert accounts.identities(busy["id"]) == ["github"]

        # disconnecting: never the last way in; GitHub takes its username with it
        c.cookies.set("jfy_session", accounts.new_session(me["user"]["id"])[0])
        csrf = c.get("/api/me").json()["csrf"]
        signins = c.get("/api/me/signins").json()["signins"]
        assert {s["key"]: s["connected"] for s in signins} == {"github": True, "microsoft": True}
        assert c.delete("/api/me/signins/github").status_code == 403                      # no CSRF header
        assert c.delete("/api/me/signins/github", headers={"x-justify-csrf": csrf}).status_code == 200
        me = c.get("/api/me").json()
        assert me["providers"] == ["microsoft"] and me["user"]["login"] is None
        assert c.delete("/api/me/signins/microsoft", headers={"x-justify-csrf": csrf}).status_code == 409
