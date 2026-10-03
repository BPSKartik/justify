"""
Scan jobs: a small queue, a job store, and a cache.

A scan of a large repository takes tens of seconds, so a request starts a job and the page
(or the MCP tool) polls it. Each scan runs as its own process, with a scrubbed environment,
a wall-clock limit and a memory limit, so one pathological repository cannot take the
service down. The same commit is never scanned twice: results are cached by
(repository, commit, Justify version).
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
from .fetch import FetchError, RepoRef, clone, resolve

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, url TEXT, ref TEXT, sha TEXT, version TEXT,
    status TEXT NOT NULL, stage TEXT, created REAL, started REAL, finished REAL,
    error TEXT, error_code TEXT, result TEXT, client TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache ON scans(repo, sha, version, status);
CREATE INDEX IF NOT EXISTS idx_status ON scans(status, created);
"""

ACTIVE = ("queued", "cloning", "scanning")


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


class Jobs:
    def __init__(self, data_dir: str, workers: int = 2, queue_max: int = 30, scan_timeout: int = 600,
                 clone_timeout: int = 180, max_mb: int = 400, scan_mem_mb: int = 3072):
        self.dir = data_dir
        os.makedirs(os.path.join(data_dir, "work"), exist_ok=True)
        self.db_path = os.path.join(data_dir, "jobs.sqlite3")
        self.queue_max, self.scan_timeout = queue_max, scan_timeout
        self.clone_timeout, self.max_mb, self.scan_mem_mb = clone_timeout, max_mb, scan_mem_mb
        self.lock = threading.Lock()
        self.q: queue.Queue[str] = queue.Queue()
        with self._db() as db:
            db.executescript(SCHEMA)
            # a restart loses the queue: say so rather than leave jobs spinning forever
            db.execute("UPDATE scans SET status='failed', error=?, error_code='interrupted', finished=? "
                       "WHERE status IN ('queued','cloning','scanning')",
                       ("The service restarted during this scan. Start it again.", time.time()))
        self.clone_fn = clone            # tests replace these with local fixtures
        self.resolve_fn = resolve
        self.scan_argv = lambda repo_dir: [sys.executable, "-m", "justify.cli", "scan", repo_dir, "--json",
                                           "--no-record", "--progress"]
        for i in range(max(1, workers)):
            threading.Thread(target=self._worker, name=f"scan-worker-{i}", daemon=True).start()

    # ------------------------------------------------------------------ store

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _update(self, job_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock, self._db() as db:
            db.execute(f"UPDATE scans SET {cols} WHERE id=?", (*fields.values(), job_id))

    def get(self, job_id: str, with_result: bool = True) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM scans WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            position = 0
            if row["status"] == "queued":
                position = db.execute("SELECT COUNT(*) FROM scans WHERE status='queued' AND created < ?",
                                      (row["created"],)).fetchone()[0]
        return self._public(row, position, with_result)

    def _public(self, row: sqlite3.Row, position: int = 0, with_result: bool = True) -> dict:
        started, finished = row["started"], row["finished"]
        return {
            "id": row["id"], "repo": row["repo"], "url": row["url"], "ref": row["ref"], "sha": row["sha"],
            "status": row["status"], "position": position,
            "stage": json.loads(row["stage"]) if row["stage"] else None,
            "created": _iso(row["created"]), "started": _iso(started), "finished": _iso(finished),
            "duration_s": round(finished - started, 1) if (started and finished) else None,
            "error": row["error"], "error_code": row["error_code"],
            "result": json.loads(row["result"]) if (with_result and row["result"]) else None,
        }

    def recent(self, limit: int = 12) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT id, repo, sha, finished, result FROM scans WHERE status='done' "
                              "ORDER BY finished DESC LIMIT 200").fetchall()
        out, seen = [], set()
        for r in rows:
            if r["repo"].lower() in seen:
                continue
            seen.add(r["repo"].lower())
            res = json.loads(r["result"])
            m, a = res.get("metrics", {}), (res.get("metrics", {}).get("attribution") or {})
            ai, human = a.get("ai_lines") or 0, a.get("human_lines") or 0
            out.append({"id": r["id"], "repo": r["repo"], "sha": r["sha"], "finished": _iso(r["finished"]),
                        "jlr_percent": m.get("jlr_percent"), "files": res.get("files"), "lines": res.get("lines"),
                        "ai_share_percent": round(100.0 * ai / (ai + human), 1) if (ai + human) else None})
            if len(out) >= limit:
                break
        return out

    def counts(self) -> dict:
        with self._db() as db:
            q = db.execute("SELECT COUNT(*) FROM scans WHERE status='queued'").fetchone()[0]
            r = db.execute("SELECT COUNT(*) FROM scans WHERE status IN ('cloning','scanning')").fetchone()[0]
        return {"queue": q, "running": r}

    # ------------------------------------------------------------------ submit

    def submit(self, rr: RepoRef, client: str = "") -> tuple[dict, bool]:
        """Returns (scan, is_new). Resolves the commit first so a cached scan answers instantly."""
        sha, branch = self.resolve_fn(rr)
        slug = rr.slug
        with self.lock, self._db() as db:
            done = db.execute("SELECT * FROM scans WHERE lower(repo)=lower(?) AND sha=? AND version=? AND "
                              "status='done' ORDER BY finished DESC LIMIT 1", (slug, sha, __version__)).fetchone()
            if done:
                return self._public(done), False
            running = db.execute("SELECT * FROM scans WHERE lower(repo)=lower(?) AND sha=? AND status IN "
                                 "('queued','cloning','scanning') LIMIT 1", (slug, sha)).fetchone()
            if running:
                return self._public(running, with_result=False), False
            queued = db.execute("SELECT COUNT(*) FROM scans WHERE status='queued'").fetchone()[0]
            if queued >= self.queue_max:
                raise FetchError("The scanner is busy right now. Try again in a few minutes.", 503, "busy")
            job_id = "s_" + secrets.token_hex(5)
            db.execute("INSERT INTO scans (id, repo, url, ref, sha, version, status, created, client) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       (job_id, slug, rr.url, branch, sha, __version__, "queued", time.time(), client))
        self.q.put(job_id)
        return self.get(job_id), True

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
        job = self.get(job_id, with_result=False)
        if not job or job["status"] != "queued":
            return
        owner, name = job["repo"].split("/", 1)
        rr = RepoRef(owner, name)
        work = tempfile.mkdtemp(prefix=f"{job_id}-", dir=os.path.join(self.dir, "work"))
        started = time.time()
        try:
            self._update(job_id, status="cloning", started=started,
                         stage=json.dumps({"stage": "clone", "message": "downloading the repository and its history"}))
            dest = os.path.join(work, name)
            sha = self.clone_fn(rr, job["ref"], dest, self.clone_timeout, self.max_mb) or job["sha"]
            self._update(job_id, status="scanning", sha=sha,
                         stage=json.dumps({"stage": "ingest", "message": "reading every Python file"}))
            result = self._scan(job_id, dest, work)
            result["root"] = job["repo"]
            result["github_blob_base"] = f"https://github.com/{job['repo']}/blob/{sha}/"
            result["sha"] = sha
            self._update(job_id, status="done", finished=time.time(), result=json.dumps(result),
                         stage=json.dumps({"stage": "done", "message": "audit complete"}))
            print(json.dumps({"event": "scan_done", "id": job_id, "repo": job["repo"], "files": result.get("files"),
                              "lines": result.get("lines"), "seconds": round(time.time() - started, 1)}), flush=True)
        except FetchError as exc:
            self._update(job_id, status="failed", finished=time.time(), error=str(exc), error_code=exc.code)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _scan(self, job_id: str, repo_dir: str, work: str) -> dict:
        home = os.path.join(work, "home")
        os.makedirs(home, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LANG": "C.UTF-8",
               "JUSTIFY_HOME": os.path.join(home, ".justify"), "PYTHONDONTWRITEBYTECODE": "1",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
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
