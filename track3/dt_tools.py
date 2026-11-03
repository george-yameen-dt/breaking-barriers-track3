"""Read-only Dynatrace access for the agents through the Dynatrace MCP server (its execute-dql tool).

Every query is parsed here first (command, time window, data object, any write), so local Cedar decides on facts the
model did not write (policies/swarm.cedar: dql-read-shape). The MCP token's read-only scopes are the next fence.
The endpoint and token are configuration, never code: DT_MCP_URL and DT_MCP_TOKEN (DT_PLATFORM_TOKEN as a fallback),
which AgentCore loads from the team04/track3 secret. Skills are a packaged copy in skills/.
"""

import json
import os
import re
import urllib.request
from pathlib import Path

from contracts import SKILLS, sha256_of

DEFAULT_WINDOW_MIN = 120
SKILL_DIRS = [Path(__file__).parent / "skills",
              *sorted((Path.home() / ".claude/plugins/cache/dynatrace-for-ai/dynatrace").glob("*/skills"), reverse=True)]
LOG_KINDS = ("cell", "phone", "tower", "safety", "step")
_UNITS = {"s": 1 / 60, "m": 1, "h": 60, "d": 1440}
_WRITES = re.compile(r"\b(delete|insert|update|upsert|ingest|drop|create|alter|grant)\b", re.I)


def parse_dql(dql: str) -> dict:
    """Facts about a query for Cedar: its command, how far back it reads, what it reads, and whether it writes."""
    text = (dql or "").strip()
    first = re.match(r"(\w+)\s*([\w.]*)", text)
    verb = first.group(1) if first else ""
    data_object = first.group(2) if first and verb.lower() == "fetch" else ("metrics" if verb.lower() == "timeseries" else "")
    window = DEFAULT_WINDOW_MIN
    starts = re.findall(r"\bfrom\s*:", text)
    simple = re.fullmatch(r".*?\bfrom\s*:\s*now\(\)\s*-\s*(\d+)\s*([smhd])\s*(?:[,|].*)?", text, re.S)
    if "timeframe" in text or "[" in text or len(starts) > 1 or (starts and not simple):
        window = 99999  # a timeframe, a subquery, several or unusual start times: treat it as unbounded
    elif simple:
        window = int(round(int(simple.group(1)) * _UNITS[simple.group(2)]))
    return {"dql_verb": verb, "window_minutes": window, "data_object": data_object, "has_write": bool(_WRITES.search(text))}


def _config() -> tuple:
    return os.environ.get("DT_MCP_URL", ""), os.environ.get("DT_MCP_TOKEN") or os.environ.get("DT_PLATFORM_TOKEN", "")


def available() -> bool:
    """The Dynatrace tools are offered to the agents only when an endpoint and a token are configured."""
    url, token = _config()
    return bool(url and token)


def _rpc(url: str, token: str, session: str, body: dict) -> tuple:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}",
        **({"Mcp-Session-Id": session} if session else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw, sid = r.read().decode(), r.headers.get("Mcp-Session-Id", session)
    if "id" not in body or not raw.strip():
        return {}, sid
    data = next((line[5:].strip() for line in raw.splitlines() if line.startswith("data:")), raw) if raw.lstrip().startswith(("event:", "data:")) else raw
    return json.loads(data), sid


def query(dql: str, traceparent: str = "", max_rows: int = 40) -> dict:
    """One DQL query through the Dynatrace MCP server (execute-dql); always an answer, never an exception."""
    url, token = _config()
    if not (url and token):
        return {"dql": dql, "error": "Dynatrace MCP is not configured (DT_MCP_URL, DT_MCP_TOKEN)"}
    try:
        _, sid = _rpc(url, token, "", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "track3-agents", "version": "1"}}})
        _rpc(url, token, sid, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        reply, _ = _rpc(url, token, sid, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "execute-dql", "arguments": {"dqlQueryString": dql, "includeTypes": False}}})
        if reply.get("error"):
            return {"dql": dql, "error": f"Dynatrace MCP: {str(reply['error'])[:300]}"}
        result = reply.get("result") or {}
        text = "".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
        if result.get("isError"):
            return {"dql": dql, "error": f"Dynatrace MCP: {text[:300]}"}
        rows = json.loads(text).get("records") or []
    except Exception as e:  # a failed read is an answer the agent can work around
        return {"dql": dql, "error": f"Dynatrace MCP failed: {str(e)[:200]}"}
    return {"dql": dql, "row_count": len(rows), "rows": rows[:max_rows], "result_sha256": sha256_of(rows)}


def _window(minutes, default: int) -> int:
    try:
        return max(1, min(int(minutes), DEFAULT_WINDOW_MIN))
    except (TypeError, ValueError):
        return default


def problems(window_minutes=120) -> dict:
    # Filter on ACTIVE: closed problems get late updates and would otherwise hide a newer open one.
    return query(f"fetch dt.davis.problems, from: now()-{_window(window_minutes, 120)}m | filter event.status == \"ACTIVE\" "
                 "| sort event.start desc | limit 20 | fields display_id, event.name, event.start, event.description")


def log_patterns(kind: str, window_minutes=30) -> dict:
    if kind not in LOG_KINDS:
        return {"error": f"kind must be one of {', '.join(LOG_KINDS)}"}
    return query(f"fetch logs, from: now()-{_window(window_minutes, 30)}m | filter service.name == \"mobile-env\" "
                 f"and mobile.kind == \"{kind}\" | summarize count = count(), by: {{content}} | sort count desc | limit 15")


def load_skill(name: str, max_chars: int = 4000) -> dict:
    if name not in SKILLS:
        return {"error": f"skill must be one of {', '.join(SKILLS)}"}
    for d in SKILL_DIRS:
        f = d / name / "SKILL.md"
        if f.exists():
            return {"skill": name, "text": f.read_text()[:max_chars]}
    return {"error": f"skill {name} is not installed (Dynatrace for AI plugin)"}
