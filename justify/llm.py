"""
The model, behind one small interface.

Stages 4 and 5 need a model that answers in JSON. Which model is a deployment
choice, not a design one, so any of these works and is picked from the
environment — keys are read from environment variables and never stored:

  azure          AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT
                 (the model runs inside the customer's own Azure tenant)
  openai         JUSTIFY_LLM_BASE_URL (+ JUSTIFY_LLM_API_KEY, JUSTIFY_LLM_MODEL) —
                 any OpenAI-compatible server, including a local Ollama
  claude-cli     the Claude Code command line, if installed and signed in
                 (found on PATH, in ~/.local/bin, or at JUSTIFY_CLAUDE_BIN)

GitHub Models was retired on 30 July 2026, so it is no longer offered.

Set JUSTIFY_LLM_PROVIDER to choose explicitly; otherwise the first one
available is used. With none available, stages 4 and 5 are skipped and every
undecided unit simply stays — doubt means keep.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request


class ModelError(RuntimeError):
    pass


# where the operating system keeps its trusted certificates, for Pythons that ship without
# their own (the python.org macOS build needs "Install Certificates.command" otherwise)
SYSTEM_CA_BUNDLES = ("/etc/ssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem", "/usr/local/etc/openssl@3/cert.pem",
                     "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and not os.environ.get("SSL_CERT_FILE"):
        for bundle in SYSTEM_CA_BUNDLES:
            if os.path.isfile(bundle):
                ctx.load_verify_locations(cafile=bundle)
                break
    return ctx


def _extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ModelError(f"model did not answer in JSON: {text[:160]!r}")
        return json.loads(m.group(0))


class Model:
    name = "none"

    def ask(self, system: str, user: str) -> dict:
        raise NotImplementedError


class _HttpChat(Model):
    def __init__(self, url: str, headers: dict, model: str | None, name: str):
        self.url, self.headers, self.model, self.name = url, headers, model, name

    def ask(self, system: str, user: str) -> dict:
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0, "response_format": {"type": "json_object"}}
        if self.model:
            body["model"] = self.model
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **self.headers}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120, context=_ssl_context()) as r:
                raw, status = r.read(), r.status
            try:
                data = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                # a retired or wrong endpoint can answer 200 with an empty or HTML body
                raise ModelError(f"{self.name}: the endpoint answered HTTP {status} with "
                                 f"{len(raw)} bytes that are not JSON — check the URL and model") from None
        except urllib.error.HTTPError as exc:
            raise ModelError(f"{self.name}: HTTP {exc.code} {exc.read()[:200]!r}") from None
        except urllib.error.URLError as exc:
            raise ModelError(f"{self.name}: {exc.reason}") from None
        try:
            return _extract_json(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError) as exc:
            raise ModelError(f"{self.name}: unexpected response shape") from exc


class ClaudeCli(Model):
    name = "claude-cli"

    def __init__(self, binary: str, model: str | None = None):
        self.binary = binary
        self.model = model or os.environ.get("JUSTIFY_CLAUDE_MODEL", "haiku")

    def ask(self, system: str, user: str) -> dict:
        prompt = f"{system}\n\n{user}\n\nAnswer with the JSON object only."
        cmd = [self.binary, "-p", prompt, "--output-format", "json", "--model", self.model]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise ModelError("claude-cli: timed out") from None
        try:
            envelope = json.loads(r.stdout)
        except json.JSONDecodeError:
            envelope = None
        if r.returncode != 0 or (isinstance(envelope, dict) and envelope.get("is_error")):
            said = (envelope or {}).get("result") if isinstance(envelope, dict) else None
            raise ModelError(f"claude-cli: {said or r.stderr[-200:] or 'exit ' + str(r.returncode)} — "
                             f"sign in once with `{self.binary}` then /login, or configure another provider")
        if isinstance(envelope, dict):
            return _extract_json(envelope.get("result", ""))
        return _extract_json(r.stdout)


def from_environment() -> Model | None:
    env = os.environ
    choice = env.get("JUSTIFY_LLM_PROVIDER", "").strip().lower()

    def azure():
        ep, key, dep = env.get("AZURE_OPENAI_ENDPOINT"), env.get("AZURE_OPENAI_API_KEY"), env.get("AZURE_OPENAI_DEPLOYMENT")
        if ep and key and dep:
            ver = env.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
            url = f"{ep.rstrip('/')}/openai/deployments/{dep}/chat/completions?api-version={ver}"
            return _HttpChat(url, {"api-key": key}, None, f"azure:{dep}")

    def openai_compatible():
        base = env.get("JUSTIFY_LLM_BASE_URL")
        if base:
            headers = {"Authorization": f"Bearer {env['JUSTIFY_LLM_API_KEY']}"} if env.get("JUSTIFY_LLM_API_KEY") else {}
            return _HttpChat(f"{base.rstrip('/')}/chat/completions", headers,
                             env.get("JUSTIFY_LLM_MODEL", "gpt-4.1-mini"), f"openai:{base}")

    def claude():
        for candidate in (env.get("JUSTIFY_CLAUDE_BIN"), shutil.which("claude"),
                          os.path.expanduser("~/.local/bin/claude")):
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return ClaudeCli(candidate)

    order = {"azure": azure, "openai": openai_compatible, "claude-cli": claude}
    if choice == "github":
        raise ValueError("GitHub Models was retired on 30 July 2026. Use azure, openai (any OpenAI-compatible "
                         "server, including Ollama) or claude-cli.")
    if choice:
        fn = order.get(choice)
        return fn() if fn else None
    for fn in (azure, openai_compatible, claude):
        m = fn()
        if m:
            return m
    return None
