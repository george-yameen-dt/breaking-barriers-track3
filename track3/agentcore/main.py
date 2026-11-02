"""AgentCore Runtime entry point for the Track 3 agents (runtime track3_agents, role team04-track3-runtime).

Payload from ./track3.sh run|pause on the laptop (or a Dynatrace Workflow later):
  {"mode": "incident" | "pause", "run_id", "trace_id", "facts", "declared_s3", "commit", "operator", "scenario"}

The checkpoint runs here, next to the agents. Every record still goes to CloudWatch Logs, DynamoDB,
Dynatrace and Arize under the run's trace id; records.jsonl is copied to S3 at the end.
Tools are called through the AgentCore Gateway, where AWS's own Cedar (ENFORCE) decides again.
A person approves state changes from the laptop; the approval arrives through DynamoDB.
"""

import json
import os
import time
from pathlib import Path

import boto3

REGION = "us-east-2"
BUCKET = "bb26-p-team04-data-279760855394-use2"
APPROVALS_TABLE = "bb26-team04-track3-approvals"
APPROVAL_TIMEOUT_S = 600


def load_secrets() -> None:
    """Dynatrace logs token and Arize key for the operator-view mirrors; the official record does not need them."""
    try:
        s = json.loads(boto3.client("secretsmanager", region_name=REGION).get_secret_value(SecretId="team04/track3")["SecretString"])
        for k, v in s.items():
            os.environ.setdefault(k, v)
    except Exception as e:  # the official record (CloudWatch, DynamoDB, S3) must not depend on the mirrors
        print(f"secrets not loaded: {e}")


load_secrets()

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

import gateway  # noqa: E402
from checkpoint import Checkpoint, Refused  # noqa: E402
from incident import AGENT_MODEL, DIAGNOSE, run_incident  # noqa: E402

app = BedrockAgentCoreApp()


def wait_for_approval(run_id: str):
    ddb = boto3.client("dynamodb", region_name=REGION)

    def ask(tool: str, _args: dict):
        start = time.time()
        while time.time() - start < APPROVAL_TIMEOUT_S:
            item = ddb.get_item(TableName=APPROVALS_TABLE, Key={"run_id": {"S": run_id}, "tool": {"S": tool}},
                                ConsistentRead=True).get("Item")
            if item:
                return item["approver"]["S"], item["approved"]["BOOL"], item.get("note", {}).get("S", "")
            time.sleep(2)
        return "nobody", False, f"no answer within {APPROVAL_TIMEOUT_S} s"
    return ask


def via_gateway(run_id: str):
    def run(tool: str, args: dict) -> dict:
        return gateway.call_tool(tool, {**args, "run_id": run_id})
    return run


def incident(cp: Checkpoint, payload: dict) -> dict:
    facts = payload["facts"]
    cp.decide('Agent::"network-agent"', "read_network", 'Network::"mobile-env-medium"', {})
    cp.record("network_read", problem=facts.get("problem"), model_running=facts.get("model_running"),
              phones_dropped=facts.get("phones_dropped"), dropped_phones=[d["phone"] for d in facts.get("dropped_phones", [])],
              source="Dynatrace, read by the operator console and passed in the start request")
    run_id = cp.run_id
    r = run_incident(cp, facts, wait_for_approval(run_id), via_gateway(run_id),
                     second_wall=lambda tool: cp.second_wall("change-agent", tool, {}, via_gateway(run_id)))
    return {"outcome": r["outcome"], "verdict": r["verdict"], "diagnosis": r["diagnosis"]}


def pause(cp: Checkpoint, payload: dict) -> dict:
    operator = payload.get("operator", "operator")
    cp.set_model_status(AGENT_MODEL, "withdrawn", operator)
    try:
        cp.call_model("network-agent", AGENT_MODEL, DIAGNOSE, '{"check": "is the network healthy?"}')
        outcome = "UNEXPECTED: the call went through"
    except Refused as e:
        outcome = f"refused: {e}"
    if not payload.get("keep_paused"):
        cp.set_model_status(AGENT_MODEL, "approved", operator)
    return {"outcome": outcome}


@app.entrypoint
def invoke(payload: dict) -> dict:
    mode = payload.get("mode", "incident")
    run_id, trace_id = payload["run_id"], payload["trace_id"]
    cp = Checkpoint(run_id, trace_id, Path("/tmp/track3") / run_id)
    cp.record("run_start", declared_s3_last_modified=payload.get("declared_s3", ""), commit=payload.get("commit", ""),
              where="AgentCore Runtime track3_agents", mode=mode, operator=payload.get("operator", ""),
              **({"scenario": payload["scenario"], "test_set_sha256": payload.get("test_set_sha256", "")} if payload.get("scenario") else {}))
    try:
        result = pause(cp, payload) if mode == "pause" else incident(cp, payload)
    except Exception as e:  # a failed run is a result too: close it properly
        cp.record("run_error", error=str(e)[:500])
        result = {"error": str(e)[:500]}
    cp.record("run_end", outcome=result.get("outcome"))
    boto3.client("s3", region_name=REGION).upload_file(str(cp.local), BUCKET, f"evidence/runs/{run_id}/records.jsonl")
    return {"run_id": run_id, "trace_id": trace_id, "records": cp.seq, **result}


if __name__ == "__main__":
    app.run()
