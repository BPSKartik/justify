"""
The model, behind one small interface.

Stages 4 and 5 need a model that answers in JSON. Which model is a deployment
choice, not a design one, so any of these works and is picked from the
environment — keys are read from environment variables and never stored:

  foundry        JUSTIFY_FOUNDRY_ENDPOINT + JUSTIFY_FOUNDRY_MODELS (Azure AI Foundry, signed in
                 with `az login` — no key). Several models make a jury.
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
import time
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
    # reasoning models (Phi-4-reasoning, gpt-oss) may think out loud first
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        # a thinking model may draft the object several times; its last complete one is the answer
        candidates = _json_objects(text)
        if not candidates:
            if "{" in text:     # truncated or malformed: that juror's answer is void, the run goes on
                raise ModelError(f"model answered with broken JSON: {text[:120]!r}") from None
            raise ModelError(f"model did not answer in JSON: {text[:160]!r}") from None
        out = candidates[-1]
    if not isinstance(out, dict):
        raise ModelError(f"model answered JSON that is not an object: {text[:120]!r}")
    return out


def _json_objects(text: str) -> list[dict]:
    """Every balanced {...} in the text that parses as a JSON object, in order."""
    found, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            elif ch != "\\":
                esc = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start:i + 1])
                    if isinstance(obj, dict):
                        found.append(obj)
                except json.JSONDecodeError:
                    pass
    return found


class Model:
    name = "none"

    def ask(self, system: str, user: str) -> dict:
        raise NotImplementedError


class _HttpChat(Model):
    """An OpenAI-style chat endpoint. Models differ in what they accept — GPT-5 refuses a
    temperature, some open models refuse response_format — so a 400 that names one of those
    is retried without it, and the model is remembered as not taking it."""

    def __init__(self, url: str, headers: dict, model: str | None, name: str, auth=None):
        self.url, self.headers, self.model, self.name, self.auth = url, headers, model, name, auth
        self.timeout = int(os.environ.get("JUSTIFY_MODEL_TIMEOUT_S") or 90)
        low = (model or "").lower()
        # optional knobs: a model that rejects one by name gets the request again without it
        if re.match(r"(gpt-5|o\d)", low):
            self.options: dict = {"max_completion_tokens": 2500, "reasoning_effort": "low",
                                  "response_format": {"type": "json_object"}}
        elif "reasoning" in low or "gpt-oss" in low or "r1" in low:
            # thinking models write their reasoning first; forcing JSON mode makes them stuff
            # it inside the JSON and run out of room — let them think, then read the JSON
            self.options = {"max_tokens": 8000}
        else:
            self.options = {"max_tokens": 2500, "temperature": 0, "response_format": {"type": "json_object"}}
        self.merge_system = "reasoning" in low       # Phi-4-reasoning reads a system turn as the user's
        self.seconds = 0.0

    def _post(self, body: dict) -> dict:
        headers = {"Content-Type": "application/json", **self.headers}
        if self.auth:
            headers["Authorization"] = f"Bearer {self.auth()}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout, context=_ssl_context()) as r:
            raw, status = r.read(), r.status
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            # a retired or wrong endpoint can answer 200 with an empty or HTML body
            raise ModelError(f"{self.name}: the endpoint answered HTTP {status} with "
                             f"{len(raw)} bytes that are not JSON — check the URL and model") from None

    def ask(self, system: str, user: str) -> dict:
        start = time.monotonic()
        for attempt in range(7):
            messages = ([{"role": "user", "content": f"{system}\n\n{user}\n\nThink, then end your reply with "
                                                         "the JSON object only."}] if self.merge_system else
                        [{"role": "system", "content": system}, {"role": "user", "content": user}])
            body = {"messages": messages, **self.options}
            if self.model:
                body["model"] = self.model
            try:
                data = self._post(body)
                break
            except urllib.error.HTTPError as exc:
                said = exc.read()[:400].decode("utf-8", errors="replace")
                low = said.lower()
                named = next((k for k in self.options if k in low or (k == "response_format" and "json" in low)), None)
                if exc.code == 400 and named:
                    self.options.pop(named)
                    continue
                if exc.code == 429 and attempt < 6:
                    time.sleep(min(30, int(exc.headers.get("Retry-After") or 5 * (attempt + 1))))
                    continue
                raise ModelError(f"{self.name}: HTTP {exc.code} {said[:200]!r}") from None
            except urllib.error.URLError as exc:
                raise ModelError(f"{self.name}: {exc.reason}") from None
            except (TimeoutError, OSError) as exc:      # a slow model is one juror short, not a crash
                raise ModelError(f"{self.name}: no answer within {self.timeout} s ({exc.__class__.__name__})") from None
        else:
            raise ModelError(f"{self.name}: gave up after repeated rate limits")
        self.seconds = round(time.monotonic() - start, 1)
        try:
            return _extract_json(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"{self.name}: unexpected response shape") from exc


class EntraToken:
    """A Microsoft Entra ID token for Azure AI Foundry, from the Azure CLI's own sign-in
    (`az login`). No key is created, stored or passed around; the token is refreshed before
    it expires."""

    def __init__(self, resource: str = "https://cognitiveservices.azure.com"):
        self.resource, self.token, self.expires = resource, None, 0.0

    def __call__(self) -> str:
        if self.token and time.time() < self.expires - 300:
            return self.token
        az = shutil.which("az") or next((p for p in ("/opt/homebrew/bin/az", "/usr/local/bin/az", "/usr/bin/az")
                                         if os.path.exists(p)), None)
        if not az:
            raise ModelError("foundry: the Azure CLI (az) is not installed — sign in with `az login`")
        r = subprocess.run([az, "account", "get-access-token", "--resource", self.resource, "-o", "json"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise ModelError("foundry: no Azure sign-in — run `az login`, then try again")
        d = json.loads(r.stdout)
        self.token = d["accessToken"]
        self.expires = float(d.get("expires_on") or (time.time() + 3000))
        return self.token


class Jury(Model):
    """Several models from different makers, asked the same question independently. The
    rule for what the jury may decide lives in judge.py; this only holds the panel."""

    def __init__(self, members: list[Model], challenger: Model):
        self.members, self.challenger = members, challenger
        self.name = "jury of " + ", ".join(m.name for m in members)

    def ask(self, system: str, user: str) -> dict:          # a jury answers through judge.py
        return self.challenger.ask(system, user)


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

    def foundry():
        """Azure AI Foundry: one endpoint, many makers' models. JUSTIFY_FOUNDRY_MODELS lists the
        deployments; more than one makes a jury. "claude-cli" in the list adds Claude locally."""
        base = env.get("JUSTIFY_FOUNDRY_ENDPOINT")
        names = [n.strip() for n in env.get("JUSTIFY_FOUNDRY_MODELS", "").split(",") if n.strip()]
        if not base or not names:
            return None
        token = EntraToken()
        url = f"{base.rstrip('/')}/models/chat/completions?api-version=2024-05-01-preview"
        members: list[Model] = []
        for n in names:
            if n == "claude-cli":
                c = claude()
                if c:
                    members.append(c)
                continue
            members.append(_HttpChat(url, {}, n, n, auth=token))
        if len(members) == 1:
            return members[0]
        wanted = env.get("JUSTIFY_JURY_CHALLENGER", names[0])
        challenger = next((m for m in members if m.name == wanted), members[0])
        return Jury(members, challenger)

    def claude():
        for candidate in (env.get("JUSTIFY_CLAUDE_BIN"), shutil.which("claude"),
                          os.path.expanduser("~/.local/bin/claude")):
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return ClaudeCli(candidate)

    order = {"azure": azure, "foundry": foundry, "openai": openai_compatible, "claude-cli": claude}
    if choice == "github":
        raise ValueError("GitHub Models was retired on 30 July 2026. Use azure, openai (any OpenAI-compatible "
                         "server, including Ollama) or claude-cli.")
    if choice:
        fn = order.get(choice)
        return fn() if fn else None
    for fn in (foundry, azure, openai_compatible, claude):
        m = fn()
        if m:
            return m
    return None
