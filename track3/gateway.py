"""Calls the Track 3 AgentCore Gateway: MCP over HTTPS, signed with the caller's own AWS credentials.

AgentCore Policy (Cedar, ENFORCE) decides every tools/call at the Gateway, before the Lambda runs.
"""

import json
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = "us-east-2"
GATEWAY_URL = "https://track3-gw-vtgfbwgbqu.gateway.bedrock-agentcore.us-east-2.amazonaws.com/mcp"
TARGET = "track3-tools"


def _parse(text: str) -> dict:
    """The Gateway answers in plain JSON or as a server-sent event stream; take the last JSON message."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1]) if data else {"raw": text[:500]}


def rpc(method: str, params: dict | None = None, session: boto3.Session | None = None) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    creds = (session or boto3.Session()).get_credentials().get_frozen_credentials()
    signed = AWSRequest(method="POST", url=GATEWAY_URL, data=body,
                        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    SigV4Auth(creds, "bedrock-agentcore", REGION).add_auth(signed)
    req = urllib.request.Request(GATEWAY_URL, data=body, headers=dict(signed.headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _parse(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_status": e.code, "error": {"message": e.read().decode()[:1000]}}


def call_tool(tool: str, args: dict, session: boto3.Session | None = None) -> dict:
    """Returns {"allowed": bool, "text": str, "raw": dict}. A Cedar deny at the Gateway comes back as an error."""
    r = rpc("tools/call", {"name": f"{TARGET}___{tool}", "arguments": args}, session)
    result = r.get("result") or {}
    text = " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    allowed = bool(result) and not result.get("isError")
    return {"allowed": allowed, "text": text or (r.get("error") or {}).get("message", ""), "raw": r}


def list_tools(session: boto3.Session | None = None) -> list:
    return [t["name"] for t in (rpc("tools/list", {}, session).get("result") or {}).get("tools", [])]


if __name__ == "__main__":
    print(json.dumps(rpc("tools/list"), indent=1)[:1500])
