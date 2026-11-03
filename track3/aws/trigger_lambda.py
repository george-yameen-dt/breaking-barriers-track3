"""Dynatrace workflow -> this Lambda (function URL) -> the orchestrator's AgentCore runtime. Fire and forget.

The workflow's HTTP action POSTs the problem as JSON with the header  x-track3-token: <shared secret>.
Step 1 (the HTTP request): check the token, build the run payload, hand it to this same function as an async invoke,
and answer 202 straight away. Step 2 (the async invoke): call InvokeAgentRuntime and wait for the orchestrator to
finish (up to 15 min, so the Lambda timeout is 900 s). Nobody reads the result; the records are in DynamoDB/CloudWatch.

Body fields used: problem_id (required), title, fault (default outage-C: the scenario the replay network plays until a
live adapter exists), plus anything else, which is passed to the orchestrator inside the alert text.
"""

import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
ORCHESTRATOR_ARN = os.environ["ORCHESTRATOR_ARN"]
TOKEN = os.environ["TRIGGER_TOKEN"]
lam = boto3.client("lambda", region_name=REGION)
agentcore = boto3.client("bedrock-agentcore", region_name=REGION,
                         config=Config(read_timeout=900, connect_timeout=15, retries={"total_max_attempts": 1}))


def alert_text(p: dict) -> str:
    keys = ("title", "problem_id", "status", "category", "affected", "root_cause", "start")
    return "Dynatrace problem. " + "; ".join(f"{k}: {p[k]}" for k in keys if p.get(k))


def start_run(problem: dict) -> dict:
    now = datetime.now(timezone.utc)
    run_id = f"{now:%Y-%m-%dT%H-%M}-track3-dt-{str(problem['problem_id'])[:24]}"
    trace_id = uuid.uuid4().hex
    return {"mode": "swarm", "run_id": run_id, "trace_id": trace_id, "fault": problem.get("fault", "outage-C"),
            "taken_at": now.isoformat(timespec="seconds"), "pause": False, "operator": "dynatrace-workflow",
            "declared": False, "declared_s3": "", "commit": "", "alert_text": alert_text(problem)}


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
    lam.invoke(FunctionName=context.function_name, InvocationType="Event", Payload=json.dumps({"_run": payload}).encode())
    return {"statusCode": 202, "headers": {"content-type": "application/json"},
            "body": json.dumps({"run_id": payload["run_id"], "trace_id": payload["trace_id"]})}
