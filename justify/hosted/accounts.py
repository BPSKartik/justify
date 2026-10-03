"""
Accounts: who someone is, how they proved it, what they may spend, and what they have audited.

Nothing secret is stored in a form that is useful if the database leaks. Session cookies, access
tokens and refresh tokens are random values the client holds; the database keeps only their
SHA-256. A provider's own access token (GitHub's, Microsoft's) is used once to read the profile
and then dropped — Justify never holds a key to anyone's GitHub.

And only what the service needs is kept at all: a display name, a GitHub username and picture —
never an email address, never a network address. The daily allowance of someone without an
account is counted under a keyed hash of their address, and the key lives only in memory and
changes every day, so the counts cannot be turned back into addresses, by anyone.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import time

from .db import Database

SESSION_DAYS = 30
ACCESS_TTL = 3600                 # an OAuth access token lives an hour...
REFRESH_TTL = 60 * 86400          # ...and is renewed with a refresh token good for sixty days


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_id(prefix: str, n: int = 8) -> str:
    return prefix + secrets.token_hex(n)


def today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def resets_at() -> str:
    tomorrow = _dt.datetime.now(_dt.timezone.utc).date() + _dt.timedelta(days=1)
    return f"{tomorrow.isoformat()}T00:00:00Z"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Quotas:
    """New audits a day. A result already cached for that commit is free — it costs nothing to show."""

    def __init__(self):
        self.anonymous = _env_int("JUSTIFY_ANON_DAILY", 3)
        # an AI app calling without an account shares one address with all its users, so when
        # accounts are not switched on it gets a larger allowance per address
        self.anonymous_mcp = _env_int("JUSTIFY_ANON_DAILY_MCP", 25)
        self.member = _env_int("JUSTIFY_USER_DAILY", 30)
        self.concurrent = _env_int("JUSTIFY_USER_CONCURRENT", 3)
        self.admins = {a.strip().lower() for a in os.environ.get("JUSTIFY_ADMINS", "").split(",") if a.strip()}

    def daily(self, user: dict | None, channel: str = "web") -> int | None:
        if user is None:
            return self.anonymous_mcp if channel == "mcp" else self.anonymous
        if user.get("plan") == "admin":
            return None                       # no cap
        return self.member


class QuotaExceeded(Exception):
    def __init__(self, message: str, used: int, limit: int):
        super().__init__(message)
        self.used, self.limit = used, limit


class Accounts:
    def __init__(self, db: Database, quotas: Quotas | None = None):
        self.db = db
        self.quotas = quotas or Quotas()
        self._salts: dict[str, bytes] = {}      # one per day, memory only
        # what earlier code kept and this code does not: emails, Microsoft sign-in names (often an
        # email), network addresses in the usage counts and in each scan's "client"
        self.db.write("UPDATE users SET email=NULL WHERE email IS NOT NULL")
        self.db.write("UPDATE users SET login=NULL WHERE login IS NOT NULL AND id IN "
                      "(SELECT user_id FROM identities WHERE provider='microsoft')")
        self.db.write("DELETE FROM usage WHERE who LIKE 'ip:%.%' OR who LIKE 'ip:%:%' "
                      "OR who LIKE 'mcp:%.%' OR who LIKE 'mcp:%:%'")
        self.db.write("UPDATE scans SET client=substr(client, 1, instr(client, ':') - 1) WHERE client LIKE '%:%'")

    # ------------------------------------------------------------------ users

    def _public_user(self, row) -> dict | None:
        if not row:
            return None
        return {"id": row["id"], "name": row["name"], "login": row["login"], "email": row["email"],
                "avatar": row["avatar"], "plan": row["plan"], "created": row["created"]}

    def user(self, user_id: str) -> dict | None:
        return self._public_user(self.db.one("SELECT * FROM users WHERE id=?", (user_id,)))

    def sign_in(self, provider: str, subject: str, profile: dict) -> dict:
        """The account behind a provider identity — created on first sign-in, refreshed after."""
        now = time.time()
        login = (profile.get("login") or "")[:80]
        name = (profile.get("name") or login or "")[:120]
        email = (profile.get("email") or "")[:200]
        avatar = profile.get("avatar") if str(profile.get("avatar") or "").startswith("https://") else None
        # the owner list is checked now, while the provider's answer is in memory; then the email and a
        # Microsoft sign-in name (often an email) are dropped — only a GitHub username, which is public, stays
        admin = {f"{provider}:{login}".lower(), email.lower()} - {""} & self.quotas.admins
        login = login if provider in ("github", "dev") else None
        email = None
        with self.db.transaction() as tx:
            row = tx.execute("SELECT user_id FROM identities WHERE provider=? AND subject=?",
                             (provider, subject)).fetchone()
            if row:
                user_id = row["user_id"]
                tx.execute("UPDATE users SET name=?, login=?, email=COALESCE(?, email), avatar=COALESCE(?, avatar), "
                           "last_seen=? WHERE id=?", (name, login, email, avatar, now, user_id))
            else:
                user_id = new_id("u_")
                tx.execute("INSERT INTO users (id, created, name, login, email, avatar, last_seen) VALUES (?,?,?,?,?,?,?)",
                           (user_id, now, name, login, email, avatar, now))
                tx.execute("INSERT INTO identities (provider, subject, user_id, created) VALUES (?,?,?,?)",
                           (provider, subject, user_id, now))
            tx.execute("UPDATE users SET plan=? WHERE id=?", ("admin" if admin else "free", user_id))
        return self.user(user_id)

    # ------------------------------------------------------------------ browser sessions

    def new_session(self, user_id: str) -> tuple[str, str]:
        value, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(18)
        now = time.time()
        self.db.write("INSERT INTO sessions (hash, user_id, created, expires, csrf) VALUES (?,?,?,?,?)",
                      (h(value), user_id, now, now + SESSION_DAYS * 86400, csrf))
        self.db.write("DELETE FROM sessions WHERE expires < ?", (now,))
        return value, csrf

    def session(self, value: str | None) -> tuple[dict, str] | None:
        if not value or len(value) > 100:
            return None
        row = self.db.one("SELECT user_id, csrf, expires FROM sessions WHERE hash=?", (h(value),))
        if not row or row["expires"] < time.time():
            return None
        user = self.user(row["user_id"])
        return (user, row["csrf"]) if user else None

    def end_session(self, value: str | None) -> None:
        if value:
            self.db.write("DELETE FROM sessions WHERE hash=?", (h(value),))

    # ------------------------------------------------------------------ tokens

    def _issue(self, tx, user_id: str, kind: str, ttl: int | None, *, name: str = "", client_id: str = "",
               scopes: str = "audit", family: str = "", resource: str = "") -> str:
        prefix = {"pat": "jst_", "access": "jat_", "refresh": "jrt_"}[kind]
        value = prefix + secrets.token_urlsafe(32)
        now = time.time()
        tx.execute("INSERT INTO tokens (hash, id, user_id, kind, name, client_id, scopes, family, resource, created, "
                   "expires, prefix) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (h(value), new_id("t_", 6), user_id, kind, name[:60], client_id, scopes, family, resource or None,
                    now, now + ttl if ttl else None, value[:8]))
        return value

    def new_personal_token(self, user_id: str, name: str) -> dict:
        with self.db.transaction() as tx:
            count = tx.execute("SELECT COUNT(*) FROM tokens WHERE user_id=? AND kind='pat'", (user_id,)).fetchone()[0]
            if count >= 10:
                raise ValueError("You have ten tokens already. Revoke one you no longer use first.")
            value = self._issue(tx, user_id, "pat", None, name=name.strip() or "Personal token", client_id="pat")
        row = self.db.one("SELECT * FROM tokens WHERE hash=?", (h(value),))
        return {**self._public_token(row), "token": value}

    def _public_token(self, row) -> dict:
        return {"id": row["id"], "name": row["name"], "prefix": row["prefix"], "created": row["created"],
                "last_used": row["last_used"], "expires": row["expires"]}

    def personal_tokens(self, user_id: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM tokens WHERE user_id=? AND kind='pat' ORDER BY created DESC", (user_id,))
        return [self._public_token(r) for r in rows]

    def revoke_personal_token(self, user_id: str, token_id: str) -> bool:
        return self.db.write("DELETE FROM tokens WHERE user_id=? AND id=? AND kind='pat'", (user_id, token_id)) > 0

    def token(self, value: str | None, kinds: tuple[str, ...] = ("pat", "access")) -> dict | None:
        """The live token row behind a bearer value, or None."""
        if not value or len(value) > 200 or not value.startswith(("jst_", "jat_", "jrt_")):
            return None
        row = self.db.one("SELECT * FROM tokens WHERE hash=?", (h(value),))
        if not row or row["kind"] not in kinds or (row["expires"] and row["expires"] < time.time()):
            return None
        if not row["last_used"] or time.time() - row["last_used"] > 60:      # one write a minute at most
            self.db.write("UPDATE tokens SET last_used=? WHERE hash=?", (time.time(), row["hash"]))
        return dict(row)

    def token_user(self, value: str | None) -> dict | None:
        row = self.token(value)
        return self.user(row["user_id"]) if row else None

    def issue_oauth_pair(self, user_id: str, client_id: str, scopes: str, resource: str = "",
                         family: str = "") -> tuple[str, str]:
        family = family or new_id("f_", 8)
        with self.db.transaction() as tx:
            access = self._issue(tx, user_id, "access", ACCESS_TTL, client_id=client_id, scopes=scopes,
                                 family=family, resource=resource)
            refresh = self._issue(tx, user_id, "refresh", REFRESH_TTL, client_id=client_id, scopes=scopes,
                                  family=family, resource=resource)
            tx.execute("DELETE FROM tokens WHERE expires IS NOT NULL AND expires < ?", (time.time(),))
        return access, refresh

    def rotate_refresh(self, old_value: str) -> None:
        self.db.write("DELETE FROM tokens WHERE hash=?", (h(old_value),))

    def revoke_family(self, family: str) -> None:
        if family:
            self.db.write("DELETE FROM tokens WHERE family=?", (family,))

    def connected_apps(self, user_id: str) -> list[dict]:
        rows = self.db.all("SELECT t.client_id, MIN(t.created) AS since, MAX(t.last_used) AS last_used, c.info "
                           "FROM tokens t LEFT JOIN oauth_clients c ON c.client_id = t.client_id "
                           "WHERE t.user_id=? AND t.kind IN ('access','refresh') GROUP BY t.client_id "
                           "ORDER BY since DESC", (user_id,))
        out = []
        for r in rows:
            info = json.loads(r["info"]) if r["info"] else {}
            out.append({"client_id": r["client_id"], "name": (info.get("client_name") or "An AI app")[:80],
                        "redirect_host": _host((info.get("redirect_uris") or [""])[0]),
                        "since": r["since"], "last_used": r["last_used"]})
        return out

    def disconnect_app(self, user_id: str, client_id: str) -> bool:
        return self.db.write("DELETE FROM tokens WHERE user_id=? AND client_id=? AND kind IN ('access','refresh')",
                             (user_id, client_id)) > 0

    # ------------------------------------------------------------------ usage

    def _who(self, user: dict | None, ip: str, channel: str = "web") -> str:
        if user:
            return f"u:{user['id']}"
        day = today()
        salt = self._salts.get(day)
        if salt is None:
            self._salts = {day: secrets.token_bytes(32)}       # yesterday's key is forgotten
            salt = self._salts[day]
        tag = hmac.new(salt, (ip or "unknown").encode(), hashlib.sha256).hexdigest()[:24]
        return f"{'mcp' if channel == 'mcp' else 'ip'}:{tag}"

    def github_id(self, user_id: str) -> str | None:
        row = self.db.one("SELECT subject FROM identities WHERE user_id=? AND provider='github'", (user_id,))
        return row["subject"] if row else None

    def add_installation(self, user_id: str, installation_id: int, account: str) -> None:
        self.db.write("INSERT INTO github_installations (user_id, installation_id, account, created) VALUES (?,?,?,?) "
                      "ON CONFLICT(user_id, installation_id) DO UPDATE SET account=excluded.account",
                      (user_id, int(installation_id), account, time.time()))

    def installations(self, user_id: str) -> list[int]:
        return [r["installation_id"] for r in
                self.db.all("SELECT installation_id FROM github_installations WHERE user_id=?", (user_id,))]

    def identities(self, user_id: str) -> list[str]:
        return [r["provider"] for r in self.db.all("SELECT provider FROM identities WHERE user_id=?", (user_id,))]

    def usage(self, user: dict | None, ip: str = "", channel: str = "web") -> dict:
        row = self.db.one("SELECT n FROM usage WHERE who=? AND day=?", (self._who(user, ip, channel), today()))
        limit = self.quotas.daily(user, channel)
        return {"used": row["n"] if row else 0, "limit": limit, "resets_at": resets_at(),
                "concurrent": self.quotas.concurrent if user else None}

    def charge(self, user: dict | None, ip: str = "", tx=None, channel: str = "web") -> None:
        """Count one new audit against today's allowance, or refuse with the reason. Pass `tx` to
        charge inside a transaction the caller already holds (the job queue does, so the check and
        the new job land together)."""
        if tx is None:
            with self.db.transaction() as own:
                return self._charge(own, user, ip, channel)
        return self._charge(tx, user, ip, channel)

    def _charge(self, tx, user: dict | None, ip: str, channel: str) -> None:
        limit = self.quotas.daily(user, channel)
        who, day = self._who(user, ip, channel), today()
        row = tx.execute("SELECT n FROM usage WHERE who=? AND day=?", (who, day)).fetchone()
        used = row["n"] if row else 0
        if limit is not None and used >= limit:
            if user is None:
                raise QuotaExceeded(f"You have used today's {limit} free audits. Sign in for "
                                    f"{self.quotas.member} a day — results already audited stay free.", used, limit)
            raise QuotaExceeded(f"You have used all {limit} of today's audits. They reset at 00:00 UTC; "
                                "results already audited stay free to open.", used, limit)
        if user is not None:
            active = tx.execute("SELECT COUNT(*) FROM scans WHERE owner=? AND status IN ('queued','cloning','scanning')",
                                (user["id"],)).fetchone()[0]
            if active >= self.quotas.concurrent:
                raise QuotaExceeded(f"You already have {active} audits running. Wait for one to finish.",
                                    used, limit or 0)
        tx.execute("INSERT INTO usage (who, day, n) VALUES (?,?,1) ON CONFLICT(who, day) DO UPDATE SET n=n+1",
                   (who, day))
        tx.execute("DELETE FROM usage WHERE day < ?", ((_dt.date.today() - _dt.timedelta(days=60)).isoformat(),))

    # ------------------------------------------------------------------ history

    def remember(self, user: dict | None, scan_id: str, via: str) -> None:
        if user:
            self.db.write("INSERT INTO user_scans (user_id, scan_id, created, via) VALUES (?,?,?,?) "
                          "ON CONFLICT(user_id, scan_id) DO UPDATE SET created=excluded.created, via=excluded.via",
                          (user["id"], scan_id, time.time(), via))

    def forget(self, user_id: str, scan_id: str) -> bool:
        return self.db.write("DELETE FROM user_scans WHERE user_id=? AND scan_id=?", (user_id, scan_id)) > 0

    def history(self, user_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self.db.all(
            "SELECT us.scan_id, us.created AS asked, us.via, s.repo, s.kind, s.sha, s.ref, s.status, s.finished, "
            "s.started, s.summary, s.error, s.visibility FROM user_scans us JOIN scans s ON s.id = us.scan_id "
            "WHERE us.user_id=? ORDER BY us.created DESC LIMIT ? OFFSET ?", (user_id, limit, offset))
        return [{"id": r["scan_id"], "asked": r["asked"], "via": r["via"], "repo": r["repo"], "kind": r["kind"],
                 "sha": r["sha"], "ref": r["ref"], "status": r["status"], "finished": r["finished"],
                 "duration_s": round(r["finished"] - r["started"], 1) if r["finished"] and r["started"] else None,
                 "error": r["error"], "private": r["visibility"] == "private",
                 "summary": json.loads(r["summary"]) if r["summary"] else None} for r in rows]

    def delete_account(self, user_id: str) -> list[str]:
        """Erase a person: profile, sign-in identities, sessions, tokens, history. Returns the ids of
        their private scans, whose results the caller deletes too. Public audits of public
        repositories stay — they belong to the repository, not the person — but lose their owner."""
        with self.db.transaction() as tx:
            private = [r["id"] for r in tx.execute("SELECT id FROM scans WHERE owner=? AND visibility='private'",
                                                   (user_id,)).fetchall()]
            tx.execute("DELETE FROM scans WHERE owner=? AND visibility='private'", (user_id,))
            tx.execute("UPDATE scans SET owner=NULL WHERE owner=?", (user_id,))
            for table in ("user_scans", "sessions", "tokens", "oauth_codes", "github_installations"):
                tx.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
            tx.execute("DELETE FROM identities WHERE user_id=?", (user_id,))
            tx.execute("DELETE FROM usage WHERE who=?", (f"u:{user_id}",))
            tx.execute("DELETE FROM users WHERE id=?", (user_id,))
        return private

    def history_count(self, user_id: str) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM user_scans WHERE user_id=?", (user_id,))["n"]


def _host(uri: str) -> str:
    from urllib.parse import urlparse
    try:
        p = urlparse(uri)
        return p.hostname or p.scheme or ""
    except ValueError:
        return ""
