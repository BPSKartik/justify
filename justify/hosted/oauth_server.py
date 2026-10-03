"""
OAuth for MCP: how ChatGPT, Claude, Copilot or Cursor get a token to call Justify as you.

The MCP SDK serves the standard endpoints — discovery metadata, dynamic client registration,
/authorize, /token, /revoke — and calls this provider to do the work. The one step that is
Justify's own is the middle: /authorize hands over to /oauth/consent, where the person signs
in (GitHub or Microsoft) and says yes or no to the app that asked. A personal access token
made on the dashboard is accepted on /mcp as well, for clients that take a header instead.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from urllib.parse import urlparse

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, RefreshToken,
                                      RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .accounts import ACCESS_TTL, Accounts, h
from .db import Database

REQUEST_TTL = 900            # someone has fifteen minutes to sign in and approve
CODE_TTL = 300
SCOPE = "audit"
BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "blob", "about"}


def _redirect_ok(uri: str) -> bool:
    """https anywhere; http only to this machine (a desktop client's loopback listener); or an
    app's own scheme (cursor://, vscode://) — never one a browser would run as code."""
    try:
        p = urlparse(uri)
    except ValueError:
        return False
    if len(uri) > 512 or p.fragment:
        return False
    scheme = p.scheme.lower()
    if scheme == "https":
        return bool(p.hostname)
    if scheme == "http":
        return p.hostname in ("127.0.0.1", "localhost", "::1")
    return bool(re.fullmatch(r"[a-z][a-z0-9+.-]{2,40}", scheme)) and scheme not in BAD_SCHEMES


class JustifyOAuth:
    def __init__(self, db: Database, accounts: Accounts, base_url: str):
        self.db, self.accounts, self.base_url = db, accounts, base_url.rstrip("/")

    # ---------------------------------------------------------------- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.db.one("SELECT info FROM oauth_clients WHERE client_id=?", (client_id,))
        return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        if not uris or len(uris) > 10 or not all(_redirect_ok(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri",
                                    "redirect URIs must be https, a loopback http address, or an app scheme")
        if client_info.client_name and len(client_info.client_name) > 100:
            client_info.client_name = client_info.client_name[:100]
        self.db.write("INSERT INTO oauth_clients (client_id, info, created) VALUES (?,?,?)",
                      (client_info.client_id, client_info.model_dump_json(), time.time()))
        # registrations nobody ever finished are forgotten after a week
        self.db.write("DELETE FROM oauth_clients WHERE created < ? AND client_id NOT IN "
                      "(SELECT DISTINCT client_id FROM tokens WHERE client_id IS NOT NULL)", (time.time() - 7 * 86400,))

    # ---------------------------------------------------------------- authorize -> consent

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req_id = "r_" + secrets.token_urlsafe(18)
        payload = {"state": params.state, "scopes": params.scopes or [SCOPE], "code_challenge": params.code_challenge,
                   "redirect_uri": str(params.redirect_uri),
                   "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                   "resource": params.resource}
        now = time.time()
        self.db.write("DELETE FROM oauth_requests WHERE created < ?", (now - REQUEST_TTL,))
        self.db.write("INSERT INTO oauth_requests (id, client_id, params, created) VALUES (?,?,?,?)",
                      (req_id, client.client_id, json.dumps(payload), now))
        return f"{self.base_url}/oauth/consent?req={req_id}"

    def pending(self, req_id: str) -> tuple[dict, dict] | None:
        """(request, client info) for a consent screen, or None if it expired or never was."""
        if not req_id or len(req_id) > 64:
            return None
        row = self.db.one("SELECT * FROM oauth_requests WHERE id=?", (req_id,))
        if not row or row["created"] < time.time() - REQUEST_TTL:
            return None
        client = self.db.one("SELECT info FROM oauth_clients WHERE client_id=?", (row["client_id"],))
        if not client:
            return None
        return {"id": row["id"], "client_id": row["client_id"], **json.loads(row["params"])}, json.loads(client["info"])

    def decide(self, req_id: str, user_id: str, allow: bool) -> str | None:
        """Where to send the browser after the person answered: back to the app, with a code or a refusal."""
        found = self.pending(req_id)
        if not found:
            return None
        req, _ = found
        self.db.write("DELETE FROM oauth_requests WHERE id=?", (req_id,))
        if not allow:
            return construct_redirect_uri(req["redirect_uri"], error="access_denied",
                                          error_description="The person declined.", state=req["state"])
        code = secrets.token_urlsafe(32)
        self.db.write("INSERT INTO oauth_codes (hash, client_id, user_id, params, expires) VALUES (?,?,?,?,?)",
                      (h(code), req["client_id"], user_id, json.dumps(req), time.time() + CODE_TTL))
        return construct_redirect_uri(req["redirect_uri"], code=code, state=req["state"], iss=self.base_url)

    # ---------------------------------------------------------------- codes and tokens

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        row = self.db.one("SELECT * FROM oauth_codes WHERE hash=?", (h(authorization_code),))
        if not row or row["client_id"] != client.client_id or row["expires"] < time.time():
            return None
        p = json.loads(row["params"])
        return AuthorizationCode(code=authorization_code, scopes=p["scopes"], expires_at=row["expires"],
                                 client_id=row["client_id"], code_challenge=p["code_challenge"],
                                 redirect_uri=p["redirect_uri"],
                                 redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
                                 resource=p.get("resource"), subject=row["user_id"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if self.db.write("DELETE FROM oauth_codes WHERE hash=?", (h(authorization_code.code),)) != 1:
            raise TokenError("invalid_grant", "that code was already used")          # single use, even in a race
        scopes = " ".join(authorization_code.scopes or [SCOPE])
        access, refresh = self.accounts.issue_oauth_pair(authorization_code.subject, client.client_id, scopes,
                                                         authorization_code.resource or "")
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=refresh,
                          scope=scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        row = self.accounts.token(refresh_token, kinds=("refresh",))
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=(row["scopes"] or SCOPE).split(),
                            expires_at=int(row["expires"]) if row["expires"] else None,
                            resource=row["resource"], subject=row["user_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        row = self.accounts.token(refresh_token.token, kinds=("refresh",))
        if not row:
            raise TokenError("invalid_grant", "that refresh token is no longer valid")
        self.accounts.rotate_refresh(refresh_token.token)          # rotated: an old one never works twice
        granted = " ".join(scopes or refresh_token.scopes or [SCOPE])
        access, refresh = self.accounts.issue_oauth_pair(row["user_id"], client.client_id, granted,
                                                         row["resource"] or "", family=row["family"])
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=refresh,
                          scope=granted)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.accounts.token(token, kinds=("access", "pat"))
        if not row:
            return None
        return AccessToken(token=token, client_id=row["client_id"] or "pat", scopes=(row["scopes"] or SCOPE).split(),
                           expires_at=int(row["expires"]) if row["expires"] else None, resource=row["resource"],
                           subject=row["user_id"])

    async def revoke_token(self, token) -> None:
        row = self.accounts.token(token.token, kinds=("access", "refresh"))
        if row:
            self.accounts.revoke_family(row["family"])

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:      # not offered
        raise TokenError("unsupported_grant_type", "not supported")
