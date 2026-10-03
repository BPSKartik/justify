"""
The service's one database: scans, accounts, sessions, tokens, OAuth clients and usage.

SQLite, because the service is one process on one replica and a file is the simplest thing that
can be backed up whole. Full scan results are not in it — they live as compressed files beside
it (see results.py) — so the database stays small enough to snapshot every few seconds.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'github', repo TEXT NOT NULL, url TEXT, ref TEXT, sha TEXT,
    version TEXT, status TEXT NOT NULL, stage TEXT, created REAL, started REAL, finished REAL,
    error TEXT, error_code TEXT, summary TEXT, client TEXT, owner TEXT,
    visibility TEXT NOT NULL DEFAULT 'public', src TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache ON scans(repo, sha, version, status);
CREATE INDEX IF NOT EXISTS idx_status ON scans(status, created);
CREATE INDEX IF NOT EXISTS idx_owner ON scans(owner, status);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, created REAL, name TEXT, login TEXT, email TEXT, avatar TEXT,
    plan TEXT NOT NULL DEFAULT 'free', last_seen REAL
);
CREATE TABLE IF NOT EXISTS identities (
    provider TEXT NOT NULL, subject TEXT NOT NULL, user_id TEXT NOT NULL, created REAL,
    PRIMARY KEY (provider, subject)
);
CREATE TABLE IF NOT EXISTS sessions (
    hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created REAL, expires REAL, csrf TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY, id TEXT NOT NULL, user_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT,
    client_id TEXT, scopes TEXT, family TEXT, resource TEXT, created REAL, expires REAL, last_used REAL,
    prefix TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id, kind);
CREATE INDEX IF NOT EXISTS idx_tokens_family ON tokens(family);
CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS oauth_requests (id TEXT PRIMARY KEY, client_id TEXT, params TEXT, created REAL);
CREATE TABLE IF NOT EXISTS oauth_codes (
    hash TEXT PRIMARY KEY, client_id TEXT, user_id TEXT, params TEXT, expires REAL
);
CREATE TABLE IF NOT EXISTS login_states (
    state TEXT PRIMARY KEY, provider TEXT, verifier TEXT, nonce TEXT, next TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS usage (who TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,
                                  PRIMARY KEY (who, day));
CREATE TABLE IF NOT EXISTS user_scans (
    user_id TEXT NOT NULL, scan_id TEXT NOT NULL, created REAL, via TEXT, PRIMARY KEY (user_id, scan_id)
);
CREATE INDEX IF NOT EXISTS idx_user_scans ON user_scans(user_id, created);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.Lock()          # one writer at a time; readers never wait on it
        with self.use() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextlib.contextmanager
    def use(self):
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    def write(self, sql: str, args: tuple = ()) -> int:
        """One statement, serialised with every other write. Returns the rows it changed."""
        with self.lock, self.use() as db:
            return db.execute(sql, args).rowcount

    def transaction(self):
        """`with db.transaction() as tx:` — several writes that land together or not at all."""
        return _Tx(self)

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self.use() as db:
            return db.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.use() as db:
            return db.execute(sql, args).fetchall()

    def snapshot(self, dest: str) -> None:
        """A consistent copy of the whole database, taken while it is in use."""
        src = self.connect()
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()


class _Tx:
    def __init__(self, database: Database):
        self.database = database

    def __enter__(self) -> sqlite3.Connection:
        self.database.lock.acquire()
        self.conn = self.database.connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.conn.close()
            self.database.lock.release()
