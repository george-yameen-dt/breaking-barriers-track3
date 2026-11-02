"""The checkpoint for the swarm and the tool loop every agent runs in.

SwarmCheckpoint is Track 3's Checkpoint with the swarm's Cedar policies (policies/swarm.cedar) and a lock, because the
three specialists write records at the same time. run_agent is one agent's turn, on Strands (agent_loop.py): every model
call is decided by Cedar against the model registry and screened by Bedrock Guardrails on the way in and out; every tool
call is decided by Cedar and recorded; the final answer must match its contract.
"""

import re
import threading
from pathlib import Path

import cedarpy

import telemetry
from agent_loop import NETWORK, AgentFailed, ask, make_agent
from checkpoint import RECORDS_TABLE, Checkpoint
from contracts import TOOLS
from incident import parse_json

POLICIES = Path(__file__).resolve().parent.parent / "policies" / "swarm.cedar"
MAX_TURNS = 8
__all__ = ["NETWORK", "AgentFailed", "POLICIES", "SwarmCheckpoint", "cedar_decide", "run_agent"]


def cedar_decide(policies: str, ids: list, principal: str, action: str, resource: str, context: dict,
                 attrs: dict | None = None) -> tuple:
    """Cedar's decision, the @id names of the policies that made it, and any evaluation errors."""
    rtype, rid = resource.split("::", 1)
    entities = [{"uid": {"__entity": {"type": rtype, "id": rid.strip('"')}}, "attrs": attrs or {}, "parents": []}]
    result = cedarpy.is_authorized({"principal": principal, "action": f'Action::"{action}"', "resource": resource,
                                    "context": context}, policies, entities)
    errors = [str(e) for e in result.diagnostics.errors]
    # Fail closed: Cedar skips a policy whose condition errors, so a broken forbid would otherwise let a call through.
    decision = "Allow" if result.decision == cedarpy.Decision.Allow and not errors else "Deny"
    # Cedar names policies policy0, policy1... in file order; map those to our @id names.
    reasons = [ids[int(r[6:])] if r.startswith("policy") and r[6:].isdigit() and int(r[6:]) < len(ids) else r
               for r in result.diagnostics.reasons]
    if errors:
        reasons = ["a policy could not be evaluated (fail closed)"]
    return decision, reasons or (["no policy permits this call"] if decision == "Deny" else []), errors


class SwarmCheckpoint(Checkpoint):
    def __init__(self, run_id: str, trace_id: str, out_dir: Path, parent_span_id: str = ""):
        super().__init__(run_id, trace_id, out_dir, parent_span_id)
        self.swarm_policies = POLICIES.read_text()
        self.swarm_policy_ids = re.findall(r'@id\("([^"]+)"\)', self.swarm_policies)
        self._lock = threading.RLock()

    def record(self, kind: str, **fields) -> dict:
        with self._lock:
            # Five runtimes write one run's records: the numbers come from one atomic counter (item seq "0000").
            self.seq = self._next_seq() - 1  # Checkpoint.record adds the 1
            return super().record(kind, **fields)

    def _next_seq(self) -> int:
        with telemetry.quiet():  # bookkeeping, like the record's own writes
            r = self.ddb.update_item(TableName=RECORDS_TABLE, Key={"run_id": {"S": self.run_id}, "seq": {"S": "0000"}},
                                     UpdateExpression="ADD next_seq :one", ExpressionAttributeValues={":one": {"N": "1"}},
                                     ReturnValues="UPDATED_NEW")
        return int(r["Attributes"]["next_seq"]["N"])

    def evaluate(self, principal: str, action: str, resource: str, context: dict, attrs: dict | None = None, **_) -> tuple:
        return cedar_decide(self.swarm_policies, self.swarm_policy_ids, principal, action, resource, context, attrs)


def run_agent(cp, role: str, model_id: str, system: str, prompt: str, tools: dict, validate=None, context_for=None,
              max_turns: int = MAX_TURNS, max_tokens: int = 3000) -> dict:
    """One agent's turn on Strands, through the checkpoint (agent_loop.py). tools maps a tool name (contracts.TOOLS) to
    its function. The answer must match its contract; the agent gets one chance to fix its shape."""
    agent, _ = make_agent(cp, role, model_id, system, prompt, tools, TOOLS, context_for, max_turns, max_tokens)
    text = ask(agent, cp, cp.guard("INPUT", prompt, role))
    for retried in (False, True):
        answer = parse_json(text)
        problems = ["the answer is not a JSON object"] if not answer else (validate(answer) if validate else [])
        if not problems:
            return answer
        if retried:
            break
        cp.record("contract_retry", who=role, problems=problems[:5])
        text = ask(agent, cp, "Your answer does not match the required JSON: " + "; ".join(problems[:5]) + ". Reply again with JSON only.")
    cp.record("agent_failed", who=role, model_id=model_id, reason="; ".join(problems)[:500], output=text[:500])
    raise AgentFailed(f"{role}: {'; '.join(problems)[:200]}")
