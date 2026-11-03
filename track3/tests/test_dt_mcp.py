"""The agents read Dynatrace through its MCP server (execute-dql). A fake streamable-HTTP MCP server stands in for it."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import dt_tools


class FakeMcp(BaseHTTPRequestHandler):
    calls, sse, fail, structured, noisy = [], False, False, False, False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeMcp.calls.append({"method": body["method"], "auth": self.headers.get("Authorization"), "session": self.headers.get("Mcp-Session-Id"),
                              "args": (body.get("params") or {}).get("arguments")})
        if "id" not in body:
            self.send_response(202); self.end_headers(); return
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}
        elif FakeMcp.structured:
            result = {"content": [], "structuredContent": {"records": [{"display_id": "P-1"}, {"display_id": "P-2"}]}}
        elif FakeMcp.fail:
            result = {"isError": True, "content": [{"type": "text", "text": "DQL syntax error"}]}
        else:
            result = {"content": [{"type": "text", "text": json.dumps({"records": [{"display_id": "P-1"}, {"display_id": "P-2"}], "metadata": {}})}]}
        payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "s-1")
        if FakeMcp.sse:
            self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            if FakeMcp.noisy:
                self.wfile.write(b'event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}\n\n')
            self.wfile.write(f"event: message\ndata: {payload}\n\n".encode())
        else:
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(payload.encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def mcp(monkeypatch):
    FakeMcp.calls, FakeMcp.sse, FakeMcp.fail, FakeMcp.structured, FakeMcp.noisy = [], False, False, False, False
    server = HTTPServer(("127.0.0.1", 0), FakeMcp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DT_MCP_URL", f"http://127.0.0.1:{server.server_port}/mcp")
    monkeypatch.setenv("DT_MCP_TOKEN", "tok-1")
    yield FakeMcp
    server.shutdown()


def test_a_query_returns_rows_and_a_hash_to_cite(mcp):
    out = dt_tools.query("fetch dt.davis.problems, from: now()-30m | limit 2")
    assert out["row_count"] == 2 and out["rows"][0] == {"display_id": "P-1"} and out["result_sha256"].startswith("sha256:")
    assert out["dql"].startswith("fetch dt.davis.problems")


def test_the_call_carries_the_token_the_session_and_only_the_query(mcp):
    dt_tools.query("fetch logs, from: now()-5m")
    methods = [c["method"] for c in mcp.calls]
    assert methods == ["initialize", "notifications/initialized", "tools/call"]
    call = mcp.calls[-1]
    assert call["auth"] == "Bearer tok-1" and call["session"] == "s-1"
    assert call["args"] == {"dqlQueryString": "fetch logs, from: now()-5m", "includeTypes": False}


def test_an_event_stream_reply_is_read_too(mcp):
    mcp.sse = True
    assert dt_tools.query("fetch logs")["row_count"] == 2


def test_a_server_error_comes_back_as_an_error_answer_not_an_exception(mcp):
    mcp.fail = True
    out = dt_tools.query("fetch logs")
    assert "DQL syntax error" in out["error"] and "rows" not in out


def test_without_an_endpoint_the_tools_are_off_and_a_query_answers_with_an_error(monkeypatch):
    for k in ("DT_MCP_URL", "DT_MCP_TOKEN", "DT_PLATFORM_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert dt_tools.available() is False
    assert "not configured" in dt_tools.query("fetch logs")["error"]


def test_configured_means_an_endpoint_and_a_token(monkeypatch):
    monkeypatch.setenv("DT_MCP_URL", "https://x/mcp")
    monkeypatch.delenv("DT_MCP_TOKEN", raising=False)
    monkeypatch.delenv("DT_PLATFORM_TOKEN", raising=False)
    assert dt_tools.available() is False
    monkeypatch.setenv("DT_MCP_TOKEN", "t")
    assert dt_tools.available() is True


def test_an_unreachable_endpoint_is_an_error_answer(monkeypatch):
    monkeypatch.setenv("DT_MCP_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.setenv("DT_MCP_TOKEN", "t")
    assert "error" in dt_tools.query("fetch logs")


def test_a_stream_with_a_notification_before_the_result_is_read(mcp):
    mcp.sse, mcp.noisy = True, True
    assert dt_tools.query("fetch logs")["row_count"] == 2


def test_rows_in_structured_content_are_read(mcp):
    mcp.structured = True
    assert dt_tools.query("fetch logs")["row_count"] == 2


def test_a_reply_that_is_not_json_names_the_stage_and_shows_its_start(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), type("H", (BaseHTTPRequestHandler,), {
        "do_POST": lambda s: (s.rfile.read(int(s.headers["Content-Length"])), s.send_response(200), s.send_header("Content-Type", "text/html"),
                              s.end_headers(), s.wfile.write(b"<html>login required</html>")) and None,
        "log_message": lambda *a: None}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("DT_MCP_URL", f"http://127.0.0.1:{server.server_port}/mcp")
    monkeypatch.setenv("DT_MCP_TOKEN", "t")
    out = dt_tools.query("fetch logs")["error"]
    server.shutdown()
    assert "initialize" in out and "login required" in out
