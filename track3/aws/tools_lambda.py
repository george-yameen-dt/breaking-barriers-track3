"""The network tools behind the AgentCore Gateway (target track3-tools).

AgentCore Policy decides at the Gateway before this code runs: rollback_model is permitted,
restart_all_towers is forbidden, so the second branch below should never be reached.
rollback_model writes the order to DynamoDB; the laptop's simulator bridge picks it up.
"""

import os
from datetime import datetime, timezone

import boto3

TABLE = os.environ.get("CONTROL_TABLE", "bb26-team04-track3-control")
ddb = boto3.client("dynamodb")


def tool_name(context) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    return custom.get("bedrockAgentCoreToolName", "").split("___", 1)[-1]


def handler(event, context):
    tool = tool_name(context) or event.get("tool", "")
    if tool == "rollback_model":
        to = event.get("to", "v1")
        ddb.put_item(TableName=TABLE, Item={
            "id": {"S": "network"}, "policy": {"S": to}, "run_id": {"S": event.get("run_id", "")},
            "by": {"S": "track3 change-agent via AgentCore Gateway"},
            "at": {"S": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}})
        return {"result": f"radio model set to {to}; the simulator applies it on its next step"}
    if tool == "restart_all_towers":
        return {"result": "restart_all_towers is not wired to the network"}
    return {"error": f"unknown tool {tool!r}"}
