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

import change_control
import checkpoint
import dt_tools
import telemetry
from adapters.replay import ReplayNetwork
from adapters.replay import FAULTS
from adapters.twin import SCENARIOS as TWIN_SCENARIOS
from adapters.twin import open_frozen, open_twin
from adapters.twin_link import HttpLink
from agents.governance import POLICIES, AgentFailed, SwarmCheckpoint, run_agent
from agents.orchestrator import Run, run_incident
from agents.risk import assess_proposals
from agents.specialist import MODELS, investigate
from checkpoint import MAX_SPAN_TEXT, Refused
from contracts import CRITIC, ORCHESTRATOR, SPECIALISTS

REGION = "us-east-2"
ROLE = os.environ.get("AGENT_ROLE", ORCHESTRATOR)


def _runtime_arn(role: str) -> str:
    return os.environ.get(f"{role.upper()}_RUNTIME_ARN", "")


class RemoteAgents:
    """How the orchestrator reaches the other four agents: a Cedar hand-off, then their own runtime."""

    def __init__(self, payload: dict):
        # parent_span_id: the orchestrator's root span, so each agent's spans hang under it in the run's one trace
        self.base = {k: payload.get(k) for k in ("run_id", "trace_id", "fault", "taken_at", "declared", "parent_span_id",
                                                          "network", "twin_scenario", "twin_state")}
        self.client = boto3.client("bedrock-agentcore", region_name=REGION,
                                   config=Config(read_timeout=600, connect_timeout=15, retries={"total_max_attempts": 1}))

    def _invoke(self, cp, role: str, body: dict) -> dict:
        """One call to another agent's runtime, in a CHAIN span; that runtime's spans hang under this one. The trace
        context goes in the standard W3C traceparent header; the span id also goes in the payload as a fallback."""
        arn = _runtime_arn(role)
        if not arn:
            raise AgentFailed(f"no AgentCore runtime is configured for {role}")
        with cp.span(f"invoke {role}", "CHAIN", **{"track3.who": role, "track3.runtime_arn": arn,
                                                   "input.value": json.dumps(body, default=str)[:MAX_SPAN_TEXT]}) as s:
            parent = format(s.get_span_context().span_id, "016x")
            r = self.client.invoke_agent_runtime(
                agentRuntimeArn=arn, qualifier="DEFAULT", runtimeSessionId=f"{self.base['run_id']}-{role}-{uuid.uuid4().hex[:8]}",
                contentType="application/json", accept="application/json", traceParent=telemetry.traceparent(s),
                payload=json.dumps({**self.base, "parent_span_id": parent, **body}).encode())
            out = json.loads(r["response"].read().decode() or "{}")
            s.set_attribute("output.value", json.dumps(out, default=str)[:MAX_SPAN_TEXT])
            if out.get("error"):
                raise (Refused if out.get("refused") else AgentFailed)(out["error"])
            return out

    def specialist(self, run, role: str) -> dict:
        run.cp.handoff(ORCHESTRATOR, role, run.alert)
        return self._invoke(run.cp, role, {"mode": "specialist", "role": role,
                                   "snapshot_sha256": run.net.snapshot["snapshot_sha256"]})["report"]

    def critic(self, run) -> dict:
        run.cp.handoff(ORCHESTRATOR, CRITIC, f"{len(run.proposals)} proposals from {len(run.reports)} specialists")
        out = self._invoke(run.cp, CRITIC, {"mode": "critic", "reports": run.reports, "proposals": run.proposals, "checks": run.checks})
        run.critic_checked.update(out.get("critic_checked", []))
        return out["assessments"]

    def ping(self, run, role: str) -> dict:
        run.cp.handoff(ORCHESTRATOR, role, "are you available?")
        return self._invoke(run.cp, role, {"mode": "specialist", "role": role, "ping": True})


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def handle(payload: dict) -> dict:
    mode, run_id = payload["mode"], payload["run_id"]
    role = payload.get("role", ROLE)
    cp = SwarmCheckpoint(run_id, payload["trace_id"], Path("/tmp/track3") / run_id / role, payload.get("parent_span_id", ""))
    with cp.active():
        try:
            return _handle(cp, mode, role, payload)
        finally:
            cp.close()  # every runtime ends its own root span; only the orchestrator writes run_end


def network_for(cp, payload: dict, run_id: str):
    """The network this runtime sees. A worker gets a frozen copy of what the orchestrator read (twin_state); the
    orchestrator attaches to the operator's live Viavi run over HTTP (read and command only); with no twin, the replay."""
    if payload.get("twin_state"):
        return open_frozen(payload["twin_state"], payload["twin_scenario"], run_id, payload["taken_at"], cp=cp)
    if payload.get("network") == "viavi":
        return open_twin("attach", payload["twin_scenario"], run_id, payload["taken_at"], link=HttpLink(), cp=cp)
    return ReplayNetwork(payload["fault"], run_id, payload["taken_at"])


def _fault_file(payload: dict) -> Path:
    """The scenario file this run is on, known from the payload (so run_start can be the first record, before the network is read)."""
    if payload.get("network") == "viavi":
        return TWIN_SCENARIOS / f"{payload['twin_scenario']}.json"
    return FAULTS / f"{payload['fault']}.json"


def _handle(cp, mode: str, role: str, payload: dict) -> dict:
    run_id = cp.run_id
    started = ended = False
    try:
        if mode == "swarm":  # the orchestrator's run begins with run_start, then the pin: the policy loaded here must be the approved one
            cp.record("run_start", mode="swarm", where="AgentCore: one runtime per agent", fault=payload["fault"],
                      fault_sha256=_sha(_fault_file(payload)), policies_sha256=_sha(POLICIES), commit=payload.get("commit", ""),
                      problem_id=payload.get("problem_id", ""), affected_cells=payload.get("affected_cells", []),
                      declared=bool(payload.get("declared")), declared_s3_last_modified=payload.get("declared_s3", ""),
                      operator=payload.get("operator", ""), runtimes={r: _runtime_arn(r) for r in (*SPECIALISTS, CRITIC)})
            started = True
            change_control.check_policy_pin(cp, POLICIES.read_text())
        net = network_for(cp, payload, run_id)  # inside the try: a refused read of the network is a refusal, not a crash
        if mode == "swarm" and payload.get("network") == "viavi":  # the workers rebuild this network from what it has read
            payload = {**payload, "twin_state": net.twin.state()}
        if mode == "specialist":
            if payload.get("ping"):
                run_agent(cp, role, MODELS[role], 'Reply with JSON only: {"ok": true}', '{"check": "are you available?"}', {},
                          max_turns=2, max_tokens=500)
                return {"ok": True}
            if net.snapshot["snapshot_sha256"] != payload["snapshot_sha256"]:
                raise AgentFailed("the snapshot rebuilt in this runtime does not match the orchestrator's")
            return {"report": investigate(Run(cp, net, dt_tools.available()), role)}
        if mode == "critic":
            run = Run(cp, net, dt_tools.available())
            run.reports, run.proposals, run.checks = payload["reports"], payload["proposals"], payload["checks"]
            assessments = assess_proposals(run)
            return {"assessments": assessments, "critic_checked": sorted(run.critic_checked)}
        # mode "swarm": this runtime is the orchestrator
        outcome = "error"
        try:
            root_span_id = format(cp.root.get_span_context().span_id, "016x")
            result = run_incident(cp, net, use_dynatrace=dt_tools.available(), pause_operator=payload.get("operator", "operator") if payload.get("pause") else "",
                                  remote=RemoteAgents({**payload, "parent_span_id": root_span_id}),
                                  alert=payload.get("alert_text", ""))
            outcome = result.get("outcome")
            return {"run_id": run_id, "outcome": outcome}
        finally:
            cp.record("run_end", outcome=outcome)
            ended = True
    except Refused as e:
        if started and not ended:
            cp.record("run_end", outcome=f"refused: {str(e)[:200]}")
        return {"error": str(e)[:500], "refused": True}
    except Exception as e:  # a failed agent is an answer the orchestrator records, not a crash
        cp.record("run_error", who=role, error=str(e)[:500])
        return {"error": str(e)[:500]}
