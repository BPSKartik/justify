"""
Signing in: GitHub and Microsoft, by the standard OAuth authorization-code flow with PKCE.

Each provider is switched on by its own pair of settings, which the owner sets on the container
app; with neither set the service runs without accounts, exactly as before:

    JUSTIFY_GITHUB_CLIENT_ID      + JUSTIFY_GITHUB_CLIENT_SECRET
    JUSTIFY_MICROSOFT_CLIENT_ID   + JUSTIFY_MICROSOFT_CLIENT_SECRET   (+ JUSTIFY_MICROSOFT_TENANT, default common)

Only the public profile is asked for (GitHub `read:user`; Microsoft `openid profile email`) —
never repository access. The provider's token reads the profile once and is thrown away.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from ..llm import _ssl_context
from .db import Database

STATE_TTL = 600


class LoginError(Exception):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _http(url: str, data: dict | None = None, headers: dict | None = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json",
                                                          "User-Agent": "justify-login", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error_description") or ""
        except (ValueError, AttributeError):
            detail = ""
        raise LoginError(f"the provider answered {exc.code} {detail[:120]}".strip()) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LoginError(f"could not reach the provider: {exc}") from None


class Provider:
    key = ""
    label = ""
    scope = ""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id, self.client_secret = client_id, client_secret

    def authorize_url(self, redirect_uri: str, state: str, challenge: str, nonce: str) -> str:
        raise NotImplementedError

    def profile(self, code: str, redirect_uri: str, verifier: str, nonce: str) -> tuple[str, dict]:
        """(stable subject, {login, name, email, avatar}) for the person who just signed in."""
        raise NotImplementedError


class GitHub(Provider):
    key, label, scope = "github", "GitHub", "read:user"

    def authorize_url(self, redirect_uri, state, challenge, nonce):
        return "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode({
            "client_id": self.client_id, "redirect_uri": redirect_uri, "scope": self.scope, "state": state,
            "allow_signup": "true", "code_challenge": challenge, "code_challenge_method": "S256"})

    def profile(self, code, redirect_uri, verifier, nonce):
        tok = _http("https://github.com/login/oauth/access_token", {
            "client_id": self.client_id, "client_secret": self.client_secret, "code": code,
            "redirect_uri": redirect_uri, "code_verifier": verifier})
        if not tok.get("access_token"):
            raise LoginError(tok.get("error_description") or "GitHub did not return a token")
        me = _http("https://api.github.com/user", headers={"Authorization": f"Bearer {tok['access_token']}",
                                                            "X-GitHub-Api-Version": "2022-11-28"})
        if not me.get("id"):
            raise LoginError("GitHub did not return a profile")
        return str(me["id"]), {"login": me.get("login"), "name": me.get("name") or me.get("login"),
                               "email": me.get("email"), "avatar": me.get("avatar_url")}


class Microsoft(Provider):
    key, label, scope = "microsoft", "Microsoft", "openid profile email"

    def __init__(self, client_id, client_secret, tenant="common"):
        super().__init__(client_id, client_secret)
        self.tenant = tenant or "common"
        self.base = f"https://login.microsoftonline.com/{urllib.parse.quote(self.tenant)}/oauth2/v2.0"

    def authorize_url(self, redirect_uri, state, challenge, nonce):
        return f"{self.base}/authorize?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "response_mode": "query", "scope": self.scope, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"})

    def profile(self, code, redirect_uri, verifier, nonce):
        tok = _http(f"{self.base}/token", {
            "client_id": self.client_id, "client_secret": self.client_secret, "code": code,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier,
            "scope": self.scope})
        claims = _id_token_claims(tok.get("id_token") or "")
        # the token came straight from Microsoft's token endpoint over TLS, which OpenID Connect
        # accepts in place of checking its signature; the audience, expiry and nonce are still checked
        if claims.get("aud") != self.client_id or claims.get("nonce") != nonce or \
                float(claims.get("exp") or 0) < time.time() or not claims.get("oid"):
            raise LoginError("Microsoft's sign-in token did not check out")
        if not str(claims.get("iss", "")).startswith("https://login.microsoftonline.com/"):
            raise LoginError("Microsoft's sign-in token came from an unexpected issuer")
        email = claims.get("email") or (claims.get("preferred_username") if "@" in str(claims.get("preferred_username"))
                                        else None)
        return f"{claims.get('tid')}:{claims['oid']}", {
            "login": claims.get("preferred_username") or email or "", "name": claims.get("name") or email,
            "email": email, "avatar": None}


def _id_token_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def providers_from_env() -> dict[str, Provider]:
    out: dict[str, Provider] = {}
    gid, gsecret = os.environ.get("JUSTIFY_GITHUB_CLIENT_ID"), os.environ.get("JUSTIFY_GITHUB_CLIENT_SECRET")
    if gid and gsecret:
        out["github"] = GitHub(gid, gsecret)
    mid, msecret = os.environ.get("JUSTIFY_MICROSOFT_CLIENT_ID"), os.environ.get("JUSTIFY_MICROSOFT_CLIENT_SECRET")
    if mid and msecret:
        out["microsoft"] = Microsoft(mid, msecret, os.environ.get("JUSTIFY_MICROSOFT_TENANT", "common"))
    return out


def safe_next(value: str | None) -> str:
    """Where to land after signing in: a path on this site, never somewhere else."""
    v = (value or "").strip()
    if not v.startswith("/") or v.startswith("//") or "\\" in v or len(v) > 600 or any(ord(c) < 32 for c in v):
        return "/dashboard"
    return v


class LoginFlow:
    def __init__(self, db: Database, providers: dict[str, Provider], base_url: str):
        self.db, self.providers, self.base_url = db, providers, base_url.rstrip("/")

    def redirect_uri(self, key: str) -> str:
        return f"{self.base_url}/auth/{key}/callback"

    def start(self, key: str, next_path: str) -> tuple[str, str]:
        """(URL to send the browser to, state value to pin in a cookie)."""
        p = self.providers[key]
        state, verifier, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(48), secrets.token_urlsafe(16)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        now = time.time()
        self.db.write("DELETE FROM login_states WHERE created < ?", (now - STATE_TTL,))
        self.db.write("INSERT INTO login_states (state, provider, verifier, nonce, next, created) VALUES (?,?,?,?,?,?)",
                      (state, key, verifier, nonce, safe_next(next_path), now))
        return p.authorize_url(self.redirect_uri(key), state, challenge, nonce), state

    def finish(self, key: str, code: str, state: str, cookie_state: str | None) -> tuple[str, dict, str]:
        """(subject, profile, next path). The state must match the one this browser started with."""
        if not code or not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
            raise LoginError("this sign-in did not start in this browser — try again")
        row = self.db.one("SELECT * FROM login_states WHERE state=? AND provider=?", (state, key))
        self.db.write("DELETE FROM login_states WHERE state=?", (state,))
        if not row or row["created"] < time.time() - STATE_TTL:
            raise LoginError("this sign-in took too long — try again")
        subject, profile = self.providers[key].profile(code, self.redirect_uri(key), row["verifier"], row["nonce"])
        return subject, profile, row["next"]
