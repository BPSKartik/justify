"""Found before the hackathon, by auditing the live service as a visitor would: a line of
backslashes that held an audit for minutes, a room behind one address sharing one allowance
for new audits, and a repository so large its syntax trees could exhaust the server."""

import hashlib
import subprocess
import time

import pytest

from justify.facts import STRING_LIT


def test_a_line_of_backslashes_is_read_at_once():
    # a regex in a grammar file: a quote that never closes, then escapes — each one doubled the
    # ways to read the line; 28 of them took 149 s, these 26 about 10 s
    line = "const re = /^['" + "\\w" * 26 + "$/;"
    t = time.perf_counter()
    list(STRING_LIT.finditer(line))
    assert time.perf_counter() - t < 0.5


@pytest.mark.parametrize("line, strings", [
    ('call("handler.process")', ["handler.process"]),
    ("url_for('index') + url_for('about')", ["index", "about"]),
    ('say("a \\"quoted\\" word")', ['a \\"quoted\\" word']),
    ("tpl = `${name} ${other}`", ["${name} ${other}"]),
    ("x = 'it\\'s' + \"two\"", ["it\\'s", "two"]),
    ('path = "C:\\\\dir\\\\"', ["C:\\\\dir\\\\"]),
])
def test_string_literals_still_read_the_same(line, strings):
    assert [m.group(2) for m in STRING_LIT.finditer(line)] == strings


def test_past_the_ceiling_other_languages_get_copies_only_and_say_so(make_repo, monkeypatch):
    pytest.importorskip("tree_sitter_language_pack")
    from justify.engine import run
    root = make_repo({"app.py": "print(1)\n",
                      "web/a.js": "import { unused } from './b'\nexport function f() { return 1 }\n",
                      "web/b.js": "export const unused = 2\n"})
    full = run(root, record=False)
    assert "JavaScript" in full.metrics["audited"]["languages"]
    assert {r["name"]: r["audit"] for r in full.languages}["JavaScript"] == "full"
    monkeypatch.setenv("JUSTIFY_LANG_MAX_LINES", "2")
    capped = run(root, record=False)
    assert capped.metrics["audited"]["languages"] == ["Python"]
    assert not [f for f in capped.findings if f.file.endswith(".js") and f.kind != "duplicate"]
    assert {r["name"]: r["audit"] for r in capped.languages}["JavaScript"] == "copies"


# ---------------------------------------------------------------- one room, one address

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from justify.hosted.jobs import Jobs  # noqa: E402


@pytest.fixture
def room(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "app.py").write_text("import io\nprint(1)\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "start"]):
        subprocess.run(["git", *args], cwd=origin, check=True, capture_output=True, env={**env, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    for k, v in {"JUSTIFY_DEV_LOGIN": "1", "JUSTIFY_PUBLIC_URL": "http://localhost", "JUSTIFY_ANON_DAILY": "50",
                 "JUSTIFY_USER_DAILY": "50", "JUSTIFY_RATE_PER_HOUR": "500", "JUSTIFY_NEW_PER_HOUR": "2"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("JUSTIFY_PUBLIC_HOSTS", raising=False)
    from justify.hosted.app import create_app
    jobs = Jobs(str(tmp_path / "data"), workers=2)
    jobs.clone_fn = lambda rr, branch, dest, timeout, max_mb: subprocess.run(
        ["git", "clone", "-q", str(origin), dest], check=True) and None
    jobs.resolve_fn = lambda rr: (hashlib.sha1(rr.slug.lower().encode()).hexdigest(), "main")
    app = create_app(jobs)
    yield lambda: TestClient(app, base_url="http://localhost", follow_redirects=False)


def _done(c, scan_id):
    for _ in range(300):
        if c.get(f"/api/scans/{scan_id}").json().get("status") in ("done", "failed"):
            return
        time.sleep(0.1)
    raise AssertionError("scan did not finish")


def _sign_in(c, name):
    c.cookies.clear()
    assert c.get(f"/auth/dev/start?name={name}&next=/dashboard").status_code == 303
    return {"x-justify-csrf": c.get("/api/me").json()["csrf"]}


def test_people_behind_one_address_each_get_their_own_new_audits_and_cached_ones_are_free(room):
    with room() as c:
        for repo in ("owner/one", "owner/two"):                    # 2 new audits an hour, in this test
            r = c.post("/api/scans", json={"repo": repo})
            assert r.status_code == 202
            _done(c, r.json()["id"])
        r = c.post("/api/scans", json={"repo": "owner/three"})
        assert r.status_code == 429 and r.json()["code"] == "rate_limited"
        for _ in range(5):                                          # opening what is audited costs nothing
            assert c.post("/api/scans", json={"repo": "owner/one"}).status_code == 200
        for name, repo in (("alice", "owner/three"), ("bob", "owner/four")):   # the same address, signed in
            r = c.post("/api/scans", json={"repo": repo}, headers=_sign_in(c, name))
            assert r.status_code == 202, r.json()
            _done(c, r.json()["id"])


# ---------------------------------------------------------------- what people paste

from justify.hosted import fetch  # noqa: E402
from justify.hosted.fetch import FetchError, RepoRef, parse  # noqa: E402


@pytest.mark.parametrize("pasted, ref", [
    ("https://github.com/BPSKartik/dsa-python?tab=readme-ov-file", ""),
    ("https://github.com/BPSKartik/dsa-python#readme", ""),
    ("https://github.com/BPSKartik/dsa-python/pulls", ""),
    ("https://github.com/BPSKartik/dsa-python/commit/abc123", ""),
    ("https://github.com/BPSKartik/dsa-python/tree/main/src", "main/src"),
    ("https://github.com/BPSKartik/dsa-python/blob/main/README.md?plain=1", "main/README.md"),
    ("https://github.com/BPSKartik/dsa-python/tree/main/a%20b/c", "main"),
])
def test_what_people_copy_from_the_address_bar(pasted, ref):
    assert parse(pasted) == RepoRef("BPSKartik", "dsa-python", ref)


def test_a_branch_is_the_longest_leading_part_that_exists():
    a, b = "a" * 40, "b" * 40
    found = {"refs/heads/main": a, "refs/heads/release/1.2": b}
    assert fetch._pick(found, "main/src/app.py") == (a, "main")
    assert fetch._pick(found, "release/1.2/docs") == (b, "release/1.2")
    assert fetch._pick(found, "nope/x") is None


def test_a_repository_github_says_is_far_too_large_is_refused_before_it_is_queued(monkeypatch):
    calls = []

    def kb(rr, timeout):
        calls.append(rr.slug)
        return {"o/huge": 6_400_000, "o/small": 200}.get(rr.slug)
    monkeypatch.setattr(fetch, "_github_kb", kb)
    monkeypatch.setattr(fetch, "_SIZES", {})
    with pytest.raises(FetchError) as e:
        fetch.check_size(RepoRef("o", "huge"), 400)
    assert e.value.status == 413 and "own machine" in str(e.value)
    fetch.check_size(RepoRef("o", "small"), 400)
    fetch.check_size(RepoRef("o", "small"), 400)                   # asked once an hour
    fetch.check_size(RepoRef("o", "unknown"), 400)                 # GitHub did not say: no refusal
    assert calls.count("o/small") == 1


def test_a_refused_repository_costs_no_allowance(room):
    with room() as c:
        jobs = c.app.state.jobs

        def size(rr):
            if rr.slug == "owner/huge":
                raise FetchError("owner/huge is too large", 413, "too_large")
        jobs.size_fn = size
        for _ in range(3):
            r = c.post("/api/scans", json={"repo": "owner/huge"})
            assert r.status_code == 413 and r.json()["code"] == "too_large"
        assert not jobs.database.all("SELECT id FROM scans WHERE repo='owner/huge'")
        for repo in ("owner/one", "owner/two"):                    # both new audits this hour are still there
            r = c.post("/api/scans", json={"repo": repo})
            assert r.status_code == 202, r.json()
            _done(c, r.json()["id"])
