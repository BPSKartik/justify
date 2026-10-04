"""The model transport against a local fake OpenAI-compatible server (no key needed),
and the MCP server end to end through the official MCP client."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from justify.engine import run
from justify.llm import from_environment


class _FakeChat(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeChat.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                               "api_key": self.headers.get("api-key"), "body": body})
        system = body["messages"][0]["content"]
        answer = ({"refuted": False, "reason": "nothing uses it", "evidence": []} if "stage 5" in system else
                  {"verdict": "remove", "reason": "never used", "evidence": [], "confidence": 0.95})
        out = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_server():
    _FakeChat.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeChat)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_openai_compatible_transport(fake_server, make_repo, monkeypatch):
    monkeypatch.setenv("JUSTIFY_LLM_PROVIDER", "openai")
    monkeypatch.setenv("JUSTIFY_LLM_BASE_URL", fake_server + "/v1")
    monkeypatch.setenv("JUSTIFY_LLM_API_KEY", "test-key")
    model = from_environment()
    res = run(make_repo({"app.py": "import io\n"}), model=model, record=False)
    io = next(f for f in res.findings if f.name == "io")
    assert io.final == "REMOVE" and io.judgement["challenge"]["refuted"] is False
    first = _FakeChat.seen[0]
    assert first["path"] == "/v1/chat/completions" and first["auth"] == "Bearer test-key"
    assert first["body"]["response_format"] == {"type": "json_object"}


def test_azure_transport_shape(fake_server, make_repo, monkeypatch):
    monkeypatch.setenv("JUSTIFY_LLM_PROVIDER", "azure")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", fake_server)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    run(make_repo({"app.py": "import io\n"}), model=from_environment(), record=False)
    first = _FakeChat.seen[0]
    assert first["path"].startswith("/openai/deployments/gpt-4o-mini/chat/completions?api-version=")
    assert first["api_key"] == "azure-key"


def test_mcp_server_through_the_official_client(make_repo, monkeypatch):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    root = make_repo({"app.py": "import io\nimport csv\nprint(csv)\n", "check.py": "import app\n"})

    async def go(env_extra):
        env = {**os.environ, **env_extra}
        params = StdioServerParameters(command=sys.executable, args=["-m", "justify.mcp_server"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = sorted(t.name for t in (await s.list_tools()).tools)
                def body(res):
                    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
                    if sc is not None:
                        return sc.get("result", sc) if isinstance(sc, dict) else sc
                    return json.loads(res.content[0].text)
                scan = body(await s.call_tool("scan_repository", {"path": str(root)}))
                prove = body(await s.call_tool("prove_removals", {"path": str(root)}))
                return tools, scan, prove

    tools, scan, prove = asyncio.run(go({"JUSTIFY_TEST_COMMAND": f"{sys.executable} check.py"}))
    assert tools == ["history", "payoff_report", "prove_removals", "scan_repository"]
    assert any(f["name"] == "io" and f["verdict"] == "REMOVE" for f in scan["findings"])
    assert prove["proof"]["passed"] == 1
    assert "import io" in (root / "app.py").read_text()

    _, _, refused = asyncio.run(go({"JUSTIFY_TEST_COMMAND": ""}))
    assert refused["verdict"] == "KEEP"


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_over_http_with_token_and_allowed_roots(make_repo, tmp_path):
    import subprocess
    import time
    import httpx2 as httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    root = make_repo({"app.py": "import io\nx = 1\n"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "b.py").write_text("import os\nx = 1\n")
    port = _free_port()
    env = {**os.environ, "JUSTIFY_MCP_TOKEN": "s3cret", "JUSTIFY_ALLOWED_ROOTS": str(root)}
    proc = subprocess.Popen([sys.executable, "-m", "justify.cli", "mcp", "--http", "--port", str(port)],
                            env=env, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            try:
                httpx.get(url, timeout=0.5)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert httpx.post(url, json={}, timeout=5).status_code == 401          # no token, no entry

        async def go():
            client = httpx.AsyncClient(headers={"Authorization": "Bearer s3cret"}, timeout=60)
            async with streamable_http_client(url, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    names = sorted(t.name for t in (await s.list_tools()).tools)
                    ok = await s.call_tool("scan_repository", {"path": str(root)})
                    blocked = await s.call_tool("scan_repository", {"path": str(elsewhere)})
                    return names, ok, blocked

        names, ok, blocked = asyncio.run(go())
        assert names == ["history", "payoff_report", "prove_removals", "scan_repository"]
        assert not ok.isError if hasattr(ok, "isError") else not ok.is_error
        assert "outside the folders" in blocked.content[0].text
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_endpoint_answering_200_without_json_is_a_clear_error(monkeypatch):
    import http.server
    import threading as _t
    from justify.llm import ModelError, _HttpChat

    class Empty(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))   # unread bytes would reset the socket
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Empty)
    _t.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ModelError, match="not JSON"):
            _HttpChat(f"http://127.0.0.1:{srv.server_port}/v1/chat/completions", {}, "m", "retired").ask("s", "u")
    finally:
        srv.shutdown()


def test_github_models_is_gone_and_claude_binary_is_found(monkeypatch, tmp_path):
    for k in ("AZURE_OPENAI_ENDPOINT", "JUSTIFY_LLM_BASE_URL", "JUSTIFY_LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "not-used")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert from_environment() is None                        # a GitHub token no longer picks a model
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho '{}'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("JUSTIFY_CLAUDE_BIN", str(fake))
    assert from_environment().binary == str(fake)
    monkeypatch.setenv("JUSTIFY_LLM_PROVIDER", "github")
    with pytest.raises(ValueError, match="retired"):
        from_environment()


def test_mcp_judge_needs_the_owner_to_allow_it(make_repo, monkeypatch):
    from justify import mcp_server

    calls = []
    monkeypatch.setattr("justify.llm.from_environment", lambda: calls.append(1))
    root = make_repo({"app.py": "import io\nx = 1\n"})
    monkeypatch.delenv("JUSTIFY_ALLOW_JUDGE", raising=False)
    monkeypatch.delenv("JUSTIFY_ALLOWED_ROOTS", raising=False)
    out = mcp_server.scan_repository(str(root), judge=True)
    assert calls == [] and "JUSTIFY_ALLOW_JUDGE" in out["judge_note"]
    monkeypatch.setenv("JUSTIFY_ALLOW_JUDGE", "1")
    mcp_server.scan_repository(str(root), judge=True)
    assert calls == [1]


def test_broken_model_json_is_a_model_error_not_a_crash():
    from justify.llm import ModelError, _extract_json
    for bad in ['{"verdict": "keep", "reason": "unterminated', '[1, 2]', 'no json here', '<think>{x</think> {"a": ']:
        with pytest.raises(ModelError):
            _extract_json(bad)
    assert _extract_json('<think>maybe {not}</think>\n{"verdict": "keep"}') == {"verdict": "keep"}


def test_the_last_complete_json_object_is_the_answer():
    from justify.llm import _extract_json
    text = ('thinking... maybe {"verdict": "keep"} no wait. {"verdict": "remove", "reason": "a {brace} in a string", '
            '"evidence": [], "confidence": 0.9}. I will now produce final JSON.{"verdict": "remove", "confidence": 0.95}')
    assert _extract_json(text) == {"verdict": "remove", "confidence": 0.95}


def test_claude_models_can_sit_on_a_foundry_jury(monkeypatch, tmp_path):
    """Opus 5.5 cannot be deployed on a student subscription, but it can join the jury through the
    Claude Code sign-in ("claude-cli:<model>") or an Anthropic key ("anthropic:<model>")."""
    from justify.llm import ClaudeCli, Jury, _Anthropic, from_environment
    fake = tmp_path / "claude"
    log = tmp_path / "args"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log}\n"
                    "echo '{\"is_error\": false, \"result\": \"{\\\\\"verdict\\\\\": \\\\\"keep\\\\\", \\\\\"confidence\\\\\": 0.9}\"}'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("JUSTIFY_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("JUSTIFY_FOUNDRY_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("JUSTIFY_FOUNDRY_MODELS", "gpt-5.6-sol,claude-cli:claude-opus-5-5,anthropic:claude-sonnet-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("JUSTIFY_JURY_CHALLENGER", "claude-opus-5-5")
    monkeypatch.delenv("JUSTIFY_LLM_PROVIDER", raising=False)
    jury = from_environment()
    assert isinstance(jury, Jury)
    assert [m.name for m in jury.members] == ["gpt-5.6-sol", "claude-opus-5-5", "claude-sonnet-5"]
    assert isinstance(jury.members[1], ClaudeCli) and isinstance(jury.members[2], _Anthropic)
    assert jury.challenger.name == "claude-opus-5-5"
    assert jury.members[1].ask("SYSTEM", "UNIT") == {"verdict": "keep", "confidence": 0.9}
    args = log.read_text().split("\n")
    # a juror sees only what it is shown: no tools, no MCP servers, its own system prompt
    assert args[args.index("--tools") + 1] == "" and "--strict-mcp-config" in args
    assert args[args.index("--system-prompt") + 1] == "SYSTEM" and args[args.index("--model") + 1] == "claude-opus-5-5"


def test_anthropic_messages_transport(monkeypatch):
    import http.server
    import threading
    from justify.llm import _Anthropic
    seen = {}

    class Messages(http.server.BaseHTTPRequestHandler):
        calls = 0

        def do_POST(self):
            Messages.calls += 1
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.update(path=self.path, key=self.headers.get("x-api-key"), version=self.headers.get("anthropic-version"),
                        system=body["system"], model=body["model"])
            if Messages.calls == 1:                      # overloaded once, then an answer
                self.send_response(529)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            out = json.dumps({"content": [{"type": "text", "text": "Thinking done.\n{\"verdict\": \"remove\", \"confidence\": 0.8}"}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Messages)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        m = _Anthropic("claude-opus-5-5", f"http://127.0.0.1:{srv.server_port}", "k-123")
        assert m.ask("SYS", "UNIT") == {"verdict": "remove", "confidence": 0.8}
        assert seen == {"path": "/v1/messages", "key": "k-123", "version": "2023-06-01", "system": "SYS",
                        "model": "claude-opus-5-5"}
    finally:
        srv.shutdown()


def test_every_model_call_counts_its_tokens(monkeypatch):
    """The jury's cost is measured from these counts, so each transport must record them."""
    import json as _json
    from justify import llm

    chat = llm._HttpChat("http://x/chat/completions", {}, "Phi-4", "Phi-4")
    monkeypatch.setattr(chat, "_post", lambda body: {"choices": [{"message": {"content": '{"verdict": "keep"}'}}],
                                                     "usage": {"prompt_tokens": 120, "completion_tokens": 30}})
    chat.ask("s", "u")
    chat.ask("s", "u")
    assert chat.usage == {"calls": 2, "input_tokens": 240, "output_tokens": 60}

    class _Done:
        returncode, stderr = 0, ""
        stdout = _json.dumps({"result": '{"verdict": "keep"}', "total_cost_usd": 0.0123,
                              "usage": {"input_tokens": 9, "output_tokens": 40, "cache_read_input_tokens": 1000,
                                        "cache_creation_input_tokens": 500}})
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: _Done())
    cli = llm.ClaudeCli("claude", "claude-opus-5-5")
    cli.ask("s", "u")
    assert cli.usage == {"calls": 1, "input_tokens": 9, "output_tokens": 40, "cache_write_tokens": 500,
                         "cache_read_tokens": 1000, "api_equivalent_usd": 0.0123}

    jury = llm.Jury([chat, cli], chat)                  # the challenger is also a member: counted once
    assert llm.usage_of(jury) == {"Phi-4": chat.usage, "claude-opus-5-5": cli.usage}
