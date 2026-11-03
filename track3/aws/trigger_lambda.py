"""Dynatrace workflow -> this Lambda (function URL) -> the orchestrator's AgentCore runtime. Fire and forget.

The workflow's HTTP action POSTs the problem as JSON with the header  x-track3-token: <shared secret>.
Step 1 (the HTTP request): check the token, build the run payload, hand it to this same function as an async invoke,
and answer 202 straight away. Step 2 (the async invoke): call InvokeAgentRuntime and wait for the orchestrator to
finish (up to 15 min, so the Lambda timeout is 900 s). Nobody reads the result; the records are in DynamoDB/CloudWatch.

Body fields used: problem_id (required), title, description, fault (default outage-C: the scenario the replay network plays
until a live adapter exists), plus anything else, which is passed to the orchestrator inside the alert text.

One run per problem: the first request for a problem_id claims it in the records table (a conditional put); a repeat
(Dynatrace sending the same problem twice) gets the same run and trace id back with 200 and starts nothing. The trace id
is derived from the problem id, so it is the same on every attempt.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from adapters import criticality

REGION = os.environ.get("AWS_REGION", "us-east-2")
ORCHESTRATOR_ARN = os.environ["ORCHESTRATOR_ARN"]
TOKEN = os.environ["TRIGGER_TOKEN"]
RECORDS_TABLE = os.environ.get("RECORDS_TABLE", "bb26-team04-track3-records")
CELL_NAME = re.compile(r"\b[A-Z]\d+/[A-Z]\d+/[A-Z]\d+\b")
lam = boto3.client("lambda", region_name=REGION)
ddb = boto3.client("dynamodb", region_name=REGION)
agentcore = boto3.client("bedrock-agentcore", region_name=REGION,
                         config=Config(read_timeout=900, connect_timeout=15, retries={"total_max_attempts": 1}))


def affected_cells(p: dict) -> list:
    """The Viavi cell names the problem is about: given, or found in its description and title (our detectors name the cell)."""
    if p.get("affected_cells"):
        return [str(c) for c in p["affected_cells"]]
    found = CELL_NAME.findall(f"{p.get('description', '')} {p.get('title', '')}")
    return list(dict.fromkeys(found))


def alert_text(p: dict) -> str:
    keys = ("title", "problem_id", "status", "category", "root_cause", "start", "description")
    text = "Dynatrace problem. " + "; ".join(f"{k}: {p[k]}" for k in keys if p.get(k))
    cells = affected_cells(p)
    if cells:
        text += "; cells: " + ", ".join(f"{c} ({criticality.criticality_of(c)})" for c in cells)
    return text


def trace_id_for(problem_id: str) -> str:
    """The same problem always gets the same trace id."""
    return hashlib.sha256(str(problem_id).encode()).hexdigest()[:32]


def claim(problem_id: str, run_id: str, trace_id: str) -> dict | None:
    """None when this call claimed the problem; otherwise the run that already holds it."""
    key = {"run_id": {"S": f"problem#{problem_id}"}, "seq": {"S": "0000"}}
    try:
        ddb.put_item(TableName=RECORDS_TABLE, Item={**key, "claimed_run_id": {"S": run_id}, "trace_id": {"S": trace_id},
                                                    "claimed_at": {"S": datetime.now(timezone.utc).isoformat(timespec="seconds")}},
                     ConditionExpression="attribute_not_exists(run_id)")
        return None
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    item = ddb.get_item(TableName=RECORDS_TABLE, Key=key, ConsistentRead=True)["Item"]
    return {"run_id": item["claimed_run_id"]["S"], "trace_id": item["trace_id"]["S"]}


def start_run(problem: dict) -> dict:
    now = datetime.now(timezone.utc)
    run_id = f"{now:%Y-%m-%dT%H-%M}-track3-dt-{str(problem['problem_id'])[:24]}"
    return {"mode": "swarm", "run_id": run_id, "trace_id": trace_id_for(problem["problem_id"]),
            "fault": problem.get("fault", "outage-C"), "taken_at": now.isoformat(timespec="seconds"), "pause": False,
            "operator": "dynatrace-workflow", "declared": False, "declared_s3": "", "commit": "",
            "problem_id": str(problem["problem_id"]), "affected_cells": affected_cells(problem), "alert_text": alert_text(problem)}


def handler(event, context):
    if "_run" in event:  # step 2: the async self-invoke
        p = event["_run"]
        r = agentcore.invoke_agent_runtime(
            agentRuntimeArn=ORCHESTRATOR_ARN, qualifier="DEFAULT", runtimeSessionId=f"{p['run_id']}-{p['trace_id'][:12]}",
            contentType="application/json", accept="application/json", payload=json.dumps(p).encode())
        print("orchestrator returned:", r["response"].read().decode()[:500])
        return {"ok": True}

    # step 1: the HTTP request from the workflow
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if headers.get("x-track3-token") != TOKEN:
        return {"statusCode": 401, "body": "unauthorized"}
    try:
        problem = json.loads(event.get("body") or "{}")
        problem["problem_id"]
    except (ValueError, KeyError):
        return {"statusCode": 400, "body": "JSON body with problem_id required"}
    payload = start_run(problem)
    held = claim(payload["problem_id"], payload["run_id"], payload["trace_id"])
    if held:  # the same problem again: nothing new starts
        return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": json.dumps({**held, "duplicate": True})}
    lam.invoke(FunctionName=context.function_name, InvocationType="Event", Payload=json.dumps({"_run": payload}).encode())
    return {"statusCode": 202, "headers": {"content-type": "application/json"},
            "body": json.dumps({"run_id": payload["run_id"], "trace_id": payload["trace_id"]})}
