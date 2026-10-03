"""
Scan jobs: a small queue, a job store, and a cache.

A scan of a large repository takes tens of seconds, so a request starts a job and the page
(or the MCP tool) polls it. Each scan runs as its own process, with a scrubbed environment,
a wall-clock limit and a memory limit, so one pathological repository cannot take the
service down. The same commit is never scanned twice: results of public repositories are
cached by (repository, commit, Justify version), and opening a cached result costs no quota.

Two kinds of job: a public GitHub repository (cloned here, result public), and code a
signed-in person uploaded (never cloned, never cached for anyone else, result private).
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

from .. import __version__
from .db import Database
from .fetch import FetchError, RepoRef, clone, resolve
from .seal import seal
from .storage import Results

ACTIVE = ("queued", "cloning", "scanning")


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def summarize(result: dict) -> dict:
    """The few numbers a history list, a dashboard and a comparison need — kept in the database,
    so none of them has to open a full result."""
    m = result.get("metrics") or {}
    a = m.get("attribution") or {}
    code = a.get("all_code") or {}
    ai = code.get("ai_lines", a.get("ai_lines", 0)) or 0
    human = code.get("human_lines", a.get("human_lines", 0)) or 0
    counts: dict[str, int] = {}
    for f in result.get("findings", []):
        counts[f.get("verdict", "")] = counts.get(f.get("verdict", ""), 0) + 1
    langs = result.get("languages") or []
    code_langs = [l for l in langs if l.get("audit") in ("full", "copies")]
    return {
        "files": result.get("files"), "lines": result.get("lines"),
        "jlr": m.get("jlr_percent"), "dead_lines": m.get("dead_weight_lines"), "dead_units": m.get("dead_weight_units"),
        "dup_lines": m.get("duplicate_lines"), "per_1000": m.get("per_1000_lines"),
        "remove": counts.get("REMOVE", 0), "simplify": counts.get("SIMPLIFY", 0), "ambiguous": counts.get("AMBIGUOUS", 0),
        "ai_lines": ai, "human_lines": human,
        "ai_share": round(100.0 * ai / (ai + human), 1) if (ai + human) else None,
        "ai_commits": a.get("ai_commits"), "commits": a.get("commits"),
        "tools": a.get("tools") or {}, "thin_history": bool((a.get("history") or {}).get("thin")),
        "traced": bool(a),
        "languages": [{"name": l["name"], "lines": l["lines"], "audit": l.get("audit")} for l in langs[:8]],
        "code_lines": sum(l["lines"] for l in code_langs),
    }


class Jobs:
    def __init__(self, data_dir: str, workers: int = 2, queue_max: int = 30, scan_timeout: int = 600,
                 clone_timeout: int = 180, max_mb: int = 400, scan_mem_mb: int = 3072,
                 db: Database | None = None, results: Results | None = None):
        self.dir = data_dir
        os.makedirs(os.path.join(data_dir, "work"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "uploads"), exist_ok=True)
        self.database = db or Database(os.path.join(data_dir, "justify.sqlite3"))
        self.results = results or Results(os.path.join(data_dir, "results"))
        self.db_path = self.database.path
        self.queue_max, self.scan_timeout = queue_max, scan_timeout
        self.clone_timeout, self.max_mb, self.scan_mem_mb = clone_timeout, max_mb, scan_mem_mb
        self.q: queue.Queue[str] = queue.Queue()
        self.keys: dict[str, bytes] = {}     # private jobs' sealing keys: memory only, gone when the job ends
        # a restart loses the queue: say so rather than leave jobs spinning forever
        self.database.write("UPDATE scans SET status='failed', error=?, error_code='interrupted', finished=? "
                            "WHERE status IN ('queued','cloning','scanning')",
                            ("The service restarted during this scan. Start it again.", time.time()))
        # private results kept before sealing existed are deleted rather than left readable
        for row in self.database.all("SELECT id FROM scans WHERE visibility='private' AND status='done' AND sealed=0"):
            self.results.delete(row["id"])
            self.database.write("UPDATE scans SET status='failed', error_code='resealed', error=? WHERE id=?",
                                ("This private result was kept before results were sealed, so it was deleted. "
                                 "Upload the code again for a sealed result.", row["id"]))
        self.token_fn = None             # installation id -> GitHub App token, for private repositories
        self.clone_fn = clone            # tests replace these with local fixtures
        self.resolve_fn = resolve
        self.scan_argv = lambda repo_dir: [sys.executable, "-m", "justify.cli", "scan", repo_dir, "--json",
                                           "--no-record", "--progress"]
        for i in range(max(1, workers)):
            threading.Thread(target=self._worker, name=f"scan-worker-{i}", daemon=True).start()

    # ------------------------------------------------------------------ store

    def _db(self) -> sqlite3.Connection:
        return self.database.connect()

    def _update(self, job_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.database.write(f"UPDATE scans SET {cols} WHERE id=?", (*fields.values(), job_id))

    def row(self, job_id: str) -> sqlite3.Row | None:
        return self.database.one("SELECT * FROM scans WHERE id=?", (job_id,))

    def get(self, job_id: str, with_result: bool = True, viewer: str | None = None) -> dict | None:
        """A scan as the API shows it. A private scan is visible to its owner only; to anyone
        else it does not exist."""
        row = self.row(job_id)
        if not row or (row["visibility"] == "private" and viewer != row["owner"]):
            return None
        position = 0
        if row["status"] == "queued":
            position = self.database.one("SELECT COUNT(*) AS n FROM scans WHERE status='queued' AND created < ?",
                                         (row["created"],))["n"]
        return self._public(row, position, with_result)

    def _public(self, row: sqlite3.Row, position: int = 0, with_result: bool = True) -> dict:
        started, finished = row["started"], row["finished"]
        sealed = bool(row["sealed"])
        result = self.results.get(row["id"]) if (with_result and row["status"] == "done" and not sealed) else None
        return {
            "id": row["id"], "kind": row["kind"], "repo": row["repo"], "url": row["url"], "ref": row["ref"],
            "sha": row["sha"], "status": row["status"], "position": position,
            "private": row["visibility"] == "private", "sealed": sealed,
            "stage": json.loads(row["stage"]) if row["stage"] else None,
            "created": _iso(row["created"]), "started": _iso(started), "finished": _iso(finished),
            "duration_s": round(finished - started, 1) if (started and finished) else None,
            "error": row["error"], "error_code": row["error_code"],
            "summary": json.loads(row["summary"]) if row["summary"] else None,
            "result": result,
        }

    def recent(self, limit: int = 12) -> list[dict]:
        rows = self.database.all("SELECT id, repo, sha, finished, summary FROM scans WHERE status='done' "
                                 "AND visibility='public' AND kind='github' ORDER BY finished DESC LIMIT 200")
        out, seen = [], set()
        for r in rows:
            if r["repo"].lower() in seen:
                continue
            seen.add(r["repo"].lower())
            s = json.loads(r["summary"]) if r["summary"] else {}
            out.append({"id": r["id"], "repo": r["repo"], "sha": r["sha"], "finished": _iso(r["finished"]),
                        "jlr_percent": s.get("jlr"), "files": s.get("files"), "lines": s.get("lines"),
                        "code_lines": s.get("code_lines"), "languages": s.get("languages", [])[:3],
                        "ai_share_percent": s.get("ai_share")})
            if len(out) >= limit:
                break
        return out

    def counts(self) -> dict:
        q = self.database.one("SELECT COUNT(*) AS n FROM scans WHERE status='queued'")["n"]
        r = self.database.one("SELECT COUNT(*) AS n FROM scans WHERE status IN ('cloning','scanning')")["n"]
        return {"queue": q, "running": r}

    def audits_of(self, repo: str, owner: str | None = None, limit: int = 30) -> list[dict]:
        """Finished audits of one repository, newest first — what a comparison is drawn from."""
        rows = self.database.all("SELECT id, sha, finished, summary, visibility, owner, sealed FROM scans WHERE lower(repo)=lower(?) "
                                 "AND status='done' ORDER BY finished DESC LIMIT ?", (repo, limit * 3))
        out, seen = [], set()
        for r in rows:
            if r["visibility"] == "private" and r["owner"] != owner:
                continue
            if r["sha"] and r["sha"] in seen:
                continue
            seen.add(r["sha"])
            out.append({"id": r["id"], "sha": r["sha"], "finished": _iso(r["finished"]), "sealed": bool(r["sealed"]),
                        "summary": json.loads(r["summary"]) if r["summary"] else None})
            if len(out) >= limit:
                break
        return out

    def delete_private(self, job_id: str, owner: str) -> bool:
        row = self.row(job_id)
        if not row or row["visibility"] != "private" or row["owner"] != owner or row["status"] in ACTIVE:
            return False
        self.database.write("DELETE FROM scans WHERE id=?", (job_id,))
        self.results.delete(job_id)
        return True

    # ------------------------------------------------------------------ submit

    def submit(self, rr: RepoRef, client: str = "", owner: str | None = None, charge=None) -> tuple[dict, bool]:
        """Returns (scan, is_new). Resolves the commit first so a cached scan answers instantly.
        `charge(tx)` is called only when a new scan is about to be queued, inside the same
        transaction, and may refuse it by raising."""
        sha, branch = self.resolve_fn(rr)
        slug = rr.slug
        with self.database.transaction() as tx:
            done = tx.execute("SELECT * FROM scans WHERE lower(repo)=lower(?) AND sha=? AND version=? AND "
                              "status='done' AND visibility='public' ORDER BY finished DESC LIMIT 1",
                              (slug, sha, __version__)).fetchone()
            running = None if done else tx.execute(
                "SELECT * FROM scans WHERE lower(repo)=lower(?) AND sha=? AND visibility='public' AND status IN "
                "('queued','cloning','scanning') LIMIT 1", (slug, sha)).fetchone()
            if not done and not running:
                queued = tx.execute("SELECT COUNT(*) FROM scans WHERE status='queued'").fetchone()[0]
                if queued >= self.queue_max:
                    raise FetchError("The scanner is busy right now. Try again in a few minutes.", 503, "busy")
                if charge:
                    charge(tx)
                job_id = "s_" + secrets.token_hex(5)
                tx.execute("INSERT INTO scans (id, kind, repo, url, ref, sha, version, status, created, client, owner) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (job_id, "github", slug, rr.url, branch, sha, __version__, "queued", time.time(), client, owner))
        if done:
            return self._public(done), False
        if running:
            return self._public(running, with_result=False), False
        self.q.put(job_id)
        return self.get(job_id, viewer=owner), True

    def submit_private_repo(self, rr: RepoRef, owner: str, installation_id: int, key: bytes, client: str = "",
                            charge=None) -> dict:
        """Queue a private GitHub repository the person installed the Justify GitHub App on. Never
        cached for anyone, and sealed with the person's key like an upload."""
        if not key or len(key) != 32:
            raise FetchError("A private audit needs the key that seals its result.", 400, "no_key")
        token = self.token_fn(installation_id) if self.token_fn else None
        sha, branch = self.resolve_fn(rr, token=token)
        with self.database.transaction() as tx:
            queued = tx.execute("SELECT COUNT(*) FROM scans WHERE status='queued'").fetchone()[0]
            if queued >= self.queue_max:
                raise FetchError("The scanner is busy right now. Try again in a few minutes.", 503, "busy")
            if charge:
                charge(tx)
            job_id = "s_" + secrets.token_hex(5)
            tx.execute("INSERT INTO scans (id, kind, repo, url, ref, sha, version, status, created, client, owner, "
                       "visibility, inst) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, "github-private", rr.slug, rr.url, branch, sha, __version__, "queued", time.time(),
                        client, owner, "private", int(installation_id)))
        self.keys[job_id] = key
        self.q.put(job_id)
        return self.get(job_id, viewer=owner)

    def submit_upload(self, src_dir: str, name: str, owner: str, client: str = "", charge=None,
                      key: bytes | None = None) -> dict:
        """Queue code someone uploaded. It was already unpacked into `src_dir`, which this job
        now owns and deletes when it finishes. `key` seals the result (see seal.py); it is held
        in memory until the job ends and never written anywhere."""
        if not key or len(key) != 32:
            raise FetchError("A private audit needs the key that seals its result.", 400, "no_key")
        with self.database.transaction() as tx:
            queued = tx.execute("SELECT COUNT(*) FROM scans WHERE status='queued'").fetchone()[0]
            if queued >= self.queue_max:
                raise FetchError("The scanner is busy right now. Try again in a few minutes.", 503, "busy")
            if charge:
                charge(tx)
            job_id = "s_" + secrets.token_hex(5)
            tx.execute("INSERT INTO scans (id, kind, repo, version, status, created, client, owner, visibility, src) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (job_id, "upload", f"upload/{name}", __version__, "queued", time.time(), client, owner,
                        "private", src_dir))
        self.keys[job_id] = key
        self.q.put(job_id)
        return self.get(job_id, viewer=owner)

    # ------------------------------------------------------------------ work

    def _worker(self) -> None:
        while True:
            job_id = self.q.get()
            try:
                self._run(job_id)
            except Exception as exc:          # never let one job kill the worker
                self._update(job_id, status="failed", finished=time.time(), error_code="internal",
                             error="Something went wrong on our side while scanning. Try again.")
                print(json.dumps({"event": "scan_crashed", "id": job_id, "error": repr(exc)[:300]}), flush=True)

    def _run(self, job_id: str) -> None:
        row = self.row(job_id)
        if not row or row["status"] != "queued":
            return
        work = tempfile.mkdtemp(prefix=f"{job_id}-", dir=os.path.join(self.dir, "work"))
        started = time.time()
        try:
            if row["kind"] == "upload":
                dest = row["src"]
                if not dest or not os.path.isdir(dest):
                    raise FetchError("The uploaded files are gone — the service restarted. Upload them again.",
                                     410, "upload_gone")
                sha = None
                self._update(job_id, status="scanning", started=started,
                             stage=json.dumps({"stage": "ingest", "message": "reading the uploaded files"}))
            else:
                owner, name = row["repo"].split("/", 1)
                rr = RepoRef(owner, name)
                self._update(job_id, status="cloning", started=started,
                             stage=json.dumps({"stage": "clone", "message": "downloading the repository and its history"}))
                dest = os.path.join(work, name)
                extra = {}
                if row["kind"] == "github-private":
                    if not self.token_fn:
                        raise FetchError("Private repositories are not switched on here.", 400, "no_app")
                    extra["token"] = self.token_fn(row["inst"])
                sha = self.clone_fn(rr, row["ref"], dest, self.clone_timeout, self.max_mb, **extra) or row["sha"]
                extra.clear()
                self._update(job_id, status="scanning", sha=sha,
                             stage=json.dumps({"stage": "ingest", "message": "reading every file"}))
            result = self._scan(job_id, dest, work)
            result["root"] = row["repo"]
            if row["kind"] in ("github", "github-private"):
                result["github_blob_base"] = f"https://github.com/{row['repo']}/blob/{sha}/"
                result["sha"] = sha
            sealed = 0
            if row["visibility"] == "private":
                key = self.keys.pop(job_id, None)
                if key is None:
                    raise FetchError("The key that seals this private result is gone — the service restarted. "
                                     "Upload the code again.", 410, "key_gone")
                self.results.put_sealed(job_id, seal(key, job_id, result))
                del key
                sealed = 1
            else:
                self.results.put(job_id, result)
            self._update(job_id, status="done", finished=time.time(), summary=json.dumps(summarize(result)),
                         stage=json.dumps({"stage": "done", "message": "audit complete"}), src=None, sealed=sealed)
            print(json.dumps({"event": "scan_done", "id": job_id, "kind": row["kind"],
                              "repo": row["repo"] if row["kind"] == "github" else "(private)",
                              "files": result.get("files"), "lines": result.get("lines"),
                              "seconds": round(time.time() - started, 1)}), flush=True)
        except FetchError as exc:
            self._update(job_id, status="failed", finished=time.time(), error=str(exc), error_code=exc.code, src=None)
        finally:
            self.keys.pop(job_id, None)
            shutil.rmtree(work, ignore_errors=True)
            if row["kind"] == "upload" and row["src"]:
                shutil.rmtree(row["src"], ignore_errors=True)

    def _scan(self, job_id: str, repo_dir: str, work: str) -> dict:
        home = os.path.join(work, "home")
        os.makedirs(home, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LANG": "C.UTF-8",
               "JUSTIFY_HOME": os.path.join(home, ".justify"), "PYTHONDONTWRITEBYTECODE": "1",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
        out_path = os.path.join(work, "result.json")
        limit = self.scan_mem_mb * 1_048_576

        def limits():                                 # runs in the child, Linux only
            try:
                import resource
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except (ImportError, ValueError, OSError):
                pass

        with open(out_path, "w") as out:
            p = subprocess.Popen(self.scan_argv(repo_dir), stdout=out, stderr=subprocess.PIPE, text=True, env=env,
                                 start_new_session=True, preexec_fn=limits if sys.platform.startswith("linux") else None)
            reader = threading.Thread(target=self._read_progress, args=(job_id, p.stderr), daemon=True)
            reader.start()
            try:
                p.wait(timeout=self.scan_timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.wait()
                raise FetchError(f"The scan took longer than {self.scan_timeout} s and was stopped. Very large "
                                 "repositories are best scanned locally.", 504, "scan_timeout") from None
            reader.join(timeout=5)
        if p.returncode != 0:
            raise FetchError("The scan stopped unexpectedly — the repository may be too large for the hosted "
                             "service. Install Justify locally to scan it.", 500, "scan_failed")
        with open(out_path) as fh:
            result = json.load(fh)
        # nothing about the server's disk leaves the server
        for f in result.get("findings", []):
            f.pop("evidence", None)
        result.pop("hashes", None)
        return result

    def _read_progress(self, job_id: str, stream) -> None:
        for line in stream:
            line = line.strip()
            if line.startswith("{"):
                try:
                    st = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "stage" in st:
                    self._update(job_id, stage=json.dumps({"stage": st["stage"], "message": st.get("message", ""),
                                                           **{k: v for k, v in st.items()
                                                              if k in ("files", "lines", "findings")}}))
