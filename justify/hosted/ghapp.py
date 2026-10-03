"""
Private GitHub repositories, through a GitHub App the person installs on the repositories they choose.

Signing in with GitHub only reads a public profile. To audit private code, the person installs the
Justify GitHub App on their own account and picks repositories; the App can read their contents and
nothing else (Contents: read, Metadata: read). For each audit Justify asks GitHub for a token that
lasts an hour and works only on that installation, clones with it, and the result is sealed with the
person's browser key, exactly like an upload. Justify never stores a GitHub token.

An installation is tied to a Justify account only when GitHub confirms it is installed on that
person's own GitHub account (the one they signed in with) — an installation id typed into a URL
proves nothing by itself.

Settings, by name: JUSTIFY_GITHUB_APP_ID, JUSTIFY_GITHUB_APP_SLUG, JUSTIFY_GITHUB_APP_KEY (the App's
private key, PEM). Without them the feature is simply off.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

from ..llm import _ssl_context

API = "https://api.github.com"


class AppError(Exception):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class GitHubApp:
    def __init__(self, app_id: str, slug: str, pem: str):
        from cryptography.hazmat.primitives import serialization
        self.app_id, self.slug = str(app_id), slug
        self.key = serialization.load_pem_private_key(pem.replace("\\n", "\n").encode(), password=None)
        self._tokens: dict[int, tuple[str, float]] = {}
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- talking to GitHub
    def _jwt(self) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        now = int(time.time())
        head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        body = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode())
        sig = self.key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{body}.{_b64url(sig)}"

    def _call(self, method: str, path: str, bearer: str) -> dict | list:
        req = urllib.request.Request(f"{API}{path}", method=method, headers={
            "Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "justify"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise AppError("GitHub does not know that installation — it may have been removed.") from None
            raise AppError(f"GitHub answered {exc.code}.") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise AppError(f"Could not reach GitHub: {exc}") from None

    def install_url(self, state: str) -> str:
        return f"https://github.com/apps/{self.slug}/installations/new?state={state}"

    def installation(self, installation_id: int) -> dict:
        return self._call("GET", f"/app/installations/{int(installation_id)}", self._jwt())

    def token(self, installation_id: int) -> str:
        """An installation token, an hour long, kept in memory and reused until five minutes before it ends."""
        with self.lock:
            hit = self._tokens.get(installation_id)
            if hit and hit[1] > time.time() + 300:
                return hit[0]
        got = self._call("POST", f"/app/installations/{int(installation_id)}/access_tokens", self._jwt())
        token = got.get("token")
        if not token:
            raise AppError("GitHub did not issue a token for that installation.")
        with self.lock:
            self._tokens[installation_id] = (token, time.time() + 3000)
        return token

    def repos(self, installation_id: int) -> list[dict]:
        token = self.token(installation_id)
        out: list[dict] = []
        for page in range(1, 4):                                    # up to 300 repositories
            got = self._call("GET", f"/installation/repositories?per_page=100&page={page}", token)
            batch = got.get("repositories", []) if isinstance(got, dict) else []
            out += [{"full_name": r["full_name"], "name": r["name"], "private": bool(r.get("private")),
                     "description": (r.get("description") or "")[:300], "language": r.get("language") or "",
                     "stars": r.get("stargazers_count") or 0, "pushed_at": r.get("pushed_at"),
                     "fork": bool(r.get("fork")), "archived": bool(r.get("archived")),
                     "installation_id": int(installation_id)} for r in batch]
            if len(batch) < 100:
                break
        return out


def app_from_env() -> GitHubApp | None:
    app_id, slug, pem = (os.environ.get("JUSTIFY_GITHUB_APP_ID"), os.environ.get("JUSTIFY_GITHUB_APP_SLUG"),
                         os.environ.get("JUSTIFY_GITHUB_APP_KEY"))
    if not (app_id and slug and pem):
        return None
    try:
        return GitHubApp(app_id, slug, pem)
    except (ValueError, TypeError) as exc:
        print(json.dumps({"event": "github_app_config_error", "error": str(exc)[:120]}), flush=True)
        return None
