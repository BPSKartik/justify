"""The hosted service: what a stranger on the internet can and cannot make it do.
Repositories here are local git fixtures handed to the job runner through its clone hook,
so nothing touches the network except the one end-to-end test at the bottom."""

import json
import os
import shutil
import subprocess
import time

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from justify.hosted.fetch import FetchError, RepoRef, parse  # noqa: E402
from justify.hosted.jobs import Jobs  # noqa: E402
from justify.hosted.ratelimit import RateLimiter, client_ip  # noqa: E402


# ---------------------------------------------------------------- input validation

@pytest.mark.parametrize("bad", [
    "https://github.com@evil.com/a/b", "https://evil.com/a/b", "file:///etc/passwd", "git@github.com:a/b.git",
    "ssh://github.com/a/b", "https://github.com/a", "a/b/../c", "-o/x", "https://github.com:8080/a/b",
    "https://user:pw@github.com/a/b", "a/b c", "a/b\nc", "", "x" * 400, "https://github.com/a/..",
])
def test_only_plain_github_repositories_are_accepted(bad):
    with pytest.raises(FetchError):
        parse(bad)


@pytest.mark.parametrize("ref", ["--upload-pack=touch /tmp/x", "-x", "a..b", "a//b", "a.lock", "a@{1}", "a b", ".hidden"])
def test_refs_cannot_carry_options_or_tricks(ref):
    with pytest.raises(FetchError):
        parse("owner/repo", ref)


def test_accepted_forms():
    assert parse("https://github.com/PrefectHQ/fastmcp.git") == RepoRef("PrefectHQ", "fastmcp", "")
    assert parse("github.com/a-b/c.d_e/tree/release/1.2") == RepoRef("a-b", "c.d_e", "release/1.2")
    assert parse("owner/repo", "v1.0").ref == "v1.0"


def test_rate_limit_and_client_ip():
    rl = RateLimiter(2)
    assert rl.take("a")[0] and rl.take("a")[0]
    ok, wait = rl.take("a")
    assert not ok and wait > 0
    assert rl.take("b")[0]
    # the last X-Forwarded-For entry is the one the trusted proxy wrote; earlier ones are forgeable
    assert client_ip({"x-forwarded-for": "1.2.3.4, 203.0.113.9"}, "10.0.0.1") == "203.0.113.9"
    assert client_ip({}, "10.0.0.1") == "10.0.0.1"


# ---------------------------------------------------------------- jobs, with a local fixture repository

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.fixture
def origin(tmp_path):
    src = tmp_path / "origin"
    src.mkdir()
    (src / "app.py").write_text("import io\nimport json\nprint(json)\n")
    (src / "secret_link.py").symlink_to("/etc/hosts")
    _git(src, "init", "-q")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "human start")
    (src / "helper.py").write_text("def unused():\n    return 1\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "assistant\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    return src


def _jobs(tmp_path, origin, **kw):
    jobs = Jobs(str(tmp_path / "data"), workers=1, **kw)

    def fake_clone(rr, branch, dest, timeout, max_mb):
        subprocess.run(["git", "-c", "core.symlinks=false", "clone", "-q", str(origin), dest], check=True)
        return subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    sha = subprocess.run(["git", "-C", str(origin), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    jobs.clone_fn = fake_clone
    jobs.resolve_fn = lambda rr: (sha, "main")
    return jobs


def _wait(jobs, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        s = jobs.get(job_id)
        if s["status"] in ("done", "failed"):
            return s
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_job_lifecycle_cache_and_no_server_paths(tmp_path, origin):
    jobs = _jobs(tmp_path, origin)
    scan, new = jobs.submit(RepoRef("owner", "repo"))
    assert new and scan["status"] == "queued"
    again, new2 = jobs.submit(RepoRef("owner", "repo"))
    assert not new2 and again["id"] == scan["id"]                  # the same commit is one job
    done = _wait(jobs, scan["id"])
    assert done["status"] == "done", done
    res = done["result"]
    names = {f["name"] for f in res["findings"]}
    assert "io" in names and "unused" in names
    assert res["root"] == "owner/repo" and res["github_blob_base"].startswith("https://github.com/owner/repo/blob/")
    blob = json.dumps(res)
    assert str(tmp_path) not in blob                               # nothing about the server's disk leaks
    assert res["metrics"]["attribution"]["ai_commits"] == 1
    cached, new3 = jobs.submit(RepoRef("Owner", "Repo"))
    assert not new3 and cached["status"] == "done" and cached["id"] == scan["id"]
    assert not os.listdir(tmp_path / "data" / "work")              # clones are always removed
    assert jobs.recent()[0]["repo"] == "owner/repo"


def test_queue_cap_and_restart_recovery(tmp_path, origin):
    jobs = _jobs(tmp_path, origin, queue_max=0)
    with pytest.raises(FetchError) as exc:
        jobs.submit(RepoRef("o", "r"))
    assert exc.value.status == 503
    db = jobs._db()
    db.execute("INSERT INTO scans (id, repo, status, created) VALUES ('s_dead', 'o/r', 'scanning', 0)")
    again = Jobs(str(tmp_path / "data"), workers=1)
    assert again.get("s_dead")["status"] == "failed" and again.get("s_dead")["error_code"] == "interrupted"


def test_scan_timeout_kills_the_process(tmp_path, origin):
    jobs = _jobs(tmp_path, origin, scan_timeout=1)
    jobs.scan_argv = lambda repo_dir: ["sh", "-c", "sleep 30 & sleep 30; wait"]
    scan, _ = jobs.submit(RepoRef("o", "slow"))
    done = _wait(jobs, scan["id"], timeout=30)
    assert done["status"] == "failed" and done["error_code"] == "scan_timeout"
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "sleep 30"], capture_output=True, text=True).stdout.split()
    assert not left                                                # the whole process group is gone


# ---------------------------------------------------------------- the HTTP app

@pytest.fixture
def client(tmp_path, origin, monkeypatch):
    monkeypatch.setenv("JUSTIFY_RATE_PER_HOUR", "3")
    monkeypatch.setenv("JUSTIFY_PUBLIC_HOSTS", "testserver")
    from justify.hosted.app import create_app
    jobs = _jobs(tmp_path, origin)
    with TestClient(create_app(jobs)) as c:
        yield c


def test_api_status_codes_and_headers(client):
    assert client.get("/health").json()["ok"]
    r = client.get("/")
    assert r.status_code == 200 and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.post("/api/scans", content="nope", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/scans", json={"repo": "https://evil.com/a/b"}).status_code == 400
    r = client.post("/api/scans", json={"repo": "owner/repo"})
    assert r.status_code in (200, 202)
    scan_id = r.json()["id"]
    for _ in range(300):
        s = client.get(f"/api/scans/{scan_id}").json()
        if s["status"] == "done":
            break
        time.sleep(0.2)
    assert s["status"] == "done"
    md = client.get(f"/api/scans/{scan_id}/report.md")
    assert md.status_code == 200 and "Justified Line Ratio" in md.text
    assert client.get("/api/scans/s_nope").status_code == 404
    assert client.get("/api/scans/../../etc").status_code == 404
    for _ in range(3):                                             # 3 per hour in this test
        r = client.post("/api/scans", json={"repo": "owner/repo"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_mcp_over_stateless_http(client):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    hdr = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    r = client.post("/mcp", json=init, headers=hdr)
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "justify"
    # no session id needed: each request stands alone, so restarts and replicas cannot strand a client
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    headers={**hdr, "mcp-protocol-version": "2025-06-18"})
    names = sorted(t["name"] for t in r.json()["result"]["tools"])
    assert names == ["audit_code", "get_scan_result", "my_audits", "scan_github_repo"]


# ---------------------------------------------------------------- one real repository

@pytest.mark.skipif(not shutil.which("git") or os.environ.get("JUSTIFY_OFFLINE") == "1", reason="offline")
def test_real_public_repository_end_to_end(tmp_path):
    from justify.hosted.fetch import resolve
    try:
        resolve(parse("judeper/FSI-CopilotGov"))
    except FetchError:
        pytest.skip("GitHub not reachable")
    jobs = Jobs(str(tmp_path / "data"), workers=1)
    scan, _ = jobs.submit(parse("judeper/FSI-CopilotGov"))
    done = _wait(jobs, scan["id"], timeout=300)
    assert done["status"] == "done" and done["result"]["files"] > 10
