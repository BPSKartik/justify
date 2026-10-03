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
