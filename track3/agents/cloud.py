"""The swarm on AgentCore: five runtimes run the same code, and AGENT_ROLE says which agent each one is.

The orchestrator's runtime takes the run from the laptop. It hands the snapshot to the three specialist runtimes and
then to the risk critic's runtime (each hand-off is a Cedar decision, then an InvokeAgentRuntime call), and waits for a
person's decision in DynamoDB. Every runtime writes its own audit records under the same run id and trace id; the record
numbers come from one atomic counter, so the trail stays in order.
"""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

import checkpoint
from adapters.replay import ReplayNetwork
from agents.governance import POLICIES, AgentFailed, SwarmCheckpoint, run_agent
from agents.orchestrator import Run, run_incident
from agents.risk import assess_proposals
from agents.specialist import MODELS, investigate
from checkpoint import Refused
from contracts import CRITIC, ORCHESTRATOR, SPECIALISTS

REGION = "us-east-2"
APPROVALS_TABLE = "bb26-team04-track3-approvals"
APPROVAL_WAIT_S = 300  # the orchestrator's whole call must finish inside AgentCore's 15-minute request limit
ROLE = os.environ.get("AGENT_ROLE", ORCHESTRATOR)


def _runtime_arn(role: str) -> str:
    return os.environ.get(f"{role.upper()}_RUNTIME_ARN", "")


class RemoteAgents:
    """How the orchestrator reaches the other four agents: a Cedar hand-off, then their own runtime."""

    def __init__(self, payload: dict):
        self.base = {k: payload.get(k) for k in ("run_id", "trace_id", "fault", "taken_at", "declared")}
        self.client = boto3.client("bedrock-agentcore", region_name=REGION,
                                   config=Config(read_timeout=600, connect_timeout=15, retries={"total_max_attempts": 1}))

    def _invoke(self, role: str, body: dict) -> dict:
        arn = _runtime_arn(role)
        if not arn:
            raise AgentFailed(f"no AgentCore runtime is configured for {role}")
        r = self.client.invoke_agent_runtime(
            agentRuntimeArn=arn, qualifier="DEFAULT", runtimeSessionId=f"{self.base['run_id']}-{role}-{uuid.uuid4().hex[:8]}",
            contentType="application/json", accept="application/json", payload=json.dumps({**self.base, **body}).encode())
        out = json.loads(r["response"].read().decode() or "{}")
        if out.get("error"):
            raise (Refused if out.get("refused") else AgentFailed)(out["error"])
        return out

    def specialist(self, run, role: str) -> dict:
        run.cp.handoff(ORCHESTRATOR, role, run.alert)
        return self._invoke(role, {"mode": "specialist", "role": role,
                                   "snapshot_sha256": run.net.snapshot["snapshot_sha256"]})["report"]

    def critic(self, run) -> dict:
        run.cp.handoff(ORCHESTRATOR, CRITIC, f"{len(run.proposals)} proposals from {len(run.reports)} specialists")
        out = self._invoke(CRITIC, {"mode": "critic", "reports": run.reports, "proposals": run.proposals, "checks": run.checks})
        run.critic_checked.update(out.get("critic_checked", []))
        return out["assessments"]

    def ping(self, run, role: str) -> dict:
        run.cp.handoff(ORCHESTRATOR, role, "are you available?")
        return self._invoke(role, {"mode": "specialist", "role": role, "ping": True})


def wait_for_person(cp):
    """The person decides on the laptop (swarm.py --cloud); the decision arrives in DynamoDB with the hash they saw."""
    ddb = boto3.client("dynamodb", region_name=REGION)

    def ask(req: dict):
        start = time.time()
        while time.time() - start < APPROVAL_WAIT_S:
            item = ddb.get_item(TableName=APPROVALS_TABLE, ConsistentRead=True,
                                Key={"run_id": {"S": cp.run_id}, "tool": {"S": req["approval_id"]}}).get("Item")
            if item:
                return (item["approver"]["S"], item["approved"]["BOOL"], item.get("note", {}).get("S", ""),
                        item.get("channel", {}).get("S", "cli"), item.get("proposal_sha256", {}).get("S", ""))
            time.sleep(2)
        return "nobody", False, f"no answer within {APPROVAL_WAIT_S} s", "cli", ""
    return ask


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def handle(payload: dict) -> dict:
    if not payload.get("declared"):  # dev runs: no operator-view mirrors; declared runs copy every record to Dynatrace
        for k in ("DT_PLATFORM_TOKEN", "DT_TRACES_TOKEN", "ARIZE_API_KEY"):
            os.environ.pop(k, None)
        checkpoint.ARIZE_KEY_FILE = Path("/tmp/no-arize-key-for-dev-runs")
    mode, run_id = payload["mode"], payload["run_id"]
    role = payload.get("role", ROLE)
    cp = SwarmCheckpoint(run_id, payload["trace_id"], Path("/tmp/track3") / run_id / role)
    net = ReplayNetwork(payload["fault"], run_id, payload["taken_at"])
    try:
        if mode == "specialist":
            if payload.get("ping"):
                run_agent(cp, role, MODELS[role], 'Reply with JSON only: {"ok": true}', '{"check": "are you available?"}', {},
                          max_turns=2, max_tokens=500)
                return {"ok": True}
            if net.snapshot["snapshot_sha256"] != payload["snapshot_sha256"]:
                raise AgentFailed("the snapshot rebuilt in this runtime does not match the orchestrator's")
            return {"report": investigate(Run(cp, net, False), role)}
        if mode == "critic":
            run = Run(cp, net, False)
            run.reports, run.proposals, run.checks = payload["reports"], payload["proposals"], payload["checks"]
            assessments = assess_proposals(run)
            return {"assessments": assessments, "critic_checked": sorted(run.critic_checked)}
        # mode "swarm": this runtime is the orchestrator
        cp.record("run_start", mode="swarm", where="AgentCore: one runtime per agent", fault=payload["fault"],
                  fault_sha256=_sha(net.fault_path), policies_sha256=_sha(POLICIES), commit=payload.get("commit", ""),
                  declared=bool(payload.get("declared")), declared_s3_last_modified=payload.get("declared_s3", ""),
                  operator=payload.get("operator", ""), runtimes={r: _runtime_arn(r) for r in (*SPECIALISTS, CRITIC)})
        outcome = "error"
        try:
            result = run_incident(cp, net, wait_for_person(cp), pause_operator=payload.get("operator", "operator") if payload.get("pause") else "",
                                  remote=RemoteAgents(payload))
            outcome = result.get("outcome")
            return {"run_id": run_id, "outcome": outcome}
        finally:
            cp.record("run_end", outcome=outcome)
    except Refused as e:
        return {"error": str(e)[:500], "refused": True}
    except Exception as e:  # a failed agent is an answer the orchestrator records, not a crash
        cp.record("run_error", who=role, error=str(e)[:500])
        return {"error": str(e)[:500]}
