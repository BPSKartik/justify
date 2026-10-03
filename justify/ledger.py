"""
The ledger — every run, every unit, every verdict and its reason.

This is the Round 1 idea kept alive: a record of what the assistant's code has
cost, now with dead weight as a second debit beside rework. It lives in
~/.justify/ledger.db (override with JUSTIFY_HOME), outside the repository, so a
scan never writes into the code it reads.
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    started TEXT NOT NULL,
    files INTEGER, lines INTEGER,
    dead_units INTEGER, dead_lines INTEGER, jlr REAL,
    ai_dead_per_1000 REAL, human_dead_per_1000 REAL,
    model TEXT, proof TEXT, metrics TEXT
);
CREATE TABLE IF NOT EXISTS verdicts (
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT, file TEXT, line INTEGER, end_line INTEGER, name TEXT,
    verdict TEXT, final TEXT, proof TEXT, authored_by TEXT, reason TEXT, judgement TEXT
);
CREATE TABLE IF NOT EXISTS file_hashes (
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    rel TEXT, sha TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo, id);
"""


def _home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("JUSTIFY_HOME", pathlib.Path.home() / ".justify"))


class Ledger:
    def __init__(self, path: pathlib.Path | None = None):
        self.path = path or (_home() / "ledger.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.executescript(SCHEMA)

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def changed_since_last(self, repo: str, hashes: dict[str, str]) -> list[str] | None:
        with self._con() as con:
            last = con.execute("SELECT id FROM runs WHERE repo = ? ORDER BY id DESC LIMIT 1", (repo,)).fetchone()
            if not last:
                return None
            old = dict(con.execute("SELECT rel, sha FROM file_hashes WHERE run_id = ?", (last["id"],)).fetchall())
        return sorted(rel for rel, sha in hashes.items() if old.get(rel) != sha)

    def record(self, res) -> int:
        m = res.metrics
        att = m.get("attribution") or {}
        with self._con() as con:
            cur = con.execute(
                "INSERT INTO runs (repo, started, files, lines, dead_units, dead_lines, jlr, "
                "ai_dead_per_1000, human_dead_per_1000, model, proof, metrics) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (res.root, res.started, res.files, res.lines, m["dead_weight_units"], m["dead_weight_lines"],
                 m["jlr_percent"], att.get("ai_dead_per_1000"), att.get("human_dead_per_1000"),
                 (res.judging or {}).get("model"), json.dumps(res.proof) if res.proof else None, json.dumps(m)))
            run_id = int(cur.lastrowid)
            con.executemany(
                "INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, f.kind, f.file, f.line, f.end_line, f.name, f.verdict, f.final, f.proof,
                  f.authored_by, f.reason, json.dumps(f.judgement) if f.judgement else None)
                 for f in res.findings])
            con.executemany("INSERT INTO file_hashes VALUES (?,?,?)",
                            [(run_id, rel, sha) for rel, sha in res.hashes.items()])
        return run_id

    def previous_judgements(self, repo: str) -> tuple[dict[str, str], dict[str, tuple[str, dict]]]:
        """File hashes and model judgements from the last run of this repository."""
        with self._con() as con:
            last = con.execute("SELECT id FROM runs WHERE repo = ? ORDER BY id DESC LIMIT 1", (repo,)).fetchone()
            if not last:
                return {}, {}
            hashes = dict(con.execute("SELECT rel, sha FROM file_hashes WHERE run_id = ?", (last["id"],)).fetchall())
            rows = con.execute("SELECT kind, file, line, name, final, judgement FROM verdicts "
                               "WHERE run_id = ? AND judgement IS NOT NULL", (last["id"],)).fetchall()
        judged = {}
        for r in rows:
            j = json.loads(r["judgement"])
            if j.get("justify"):
                judged[f"{r['kind']}:{r['file']}:{r['line']}:{r['name']}"] = (r["final"], j)
        return hashes, judged

    def history(self, repo: str, limit: int = 30) -> list[dict]:
        with self._con() as con:
            rows = con.execute("SELECT id, started, files, lines, dead_units, dead_lines, jlr, ai_dead_per_1000, "
                               "human_dead_per_1000, model FROM runs WHERE repo = ? ORDER BY id DESC LIMIT ?",
                               (repo, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
