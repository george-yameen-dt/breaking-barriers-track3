"""The checkpoint for the swarm and the tool loop every agent runs in.

SwarmCheckpoint is Track 3's Checkpoint with the swarm's Cedar policies (policies/swarm.cedar) and a lock, because the
three specialists write records at the same time. run_agent is one agent's turn: every model call is decided by Cedar
against the model registry and screened by Bedrock Guardrails on the way in and out; every tool call is decided by
Cedar and recorded; the final answer must match its contract.
"""

import json
import re
import threading
import time
from pathlib import Path

import cedarpy

from checkpoint import RECORDS_TABLE, Checkpoint, Refused, sha256
from contracts import TOOLS
from incident import parse_json

POLICIES = Path(__file__).resolve().parent.parent / "policies" / "swarm.cedar"
NETWORK = 'Network::"mobile-env-medium"'
MAX_TURNS = 8
MAX_TOOL_RESULT = 6000


class AgentFailed(Exception):
    """An agent ran out of turns or answered outside its contract."""


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
    def __init__(self, run_id: str, trace_id: str, out_dir: Path):
        super().__init__(run_id, trace_id, out_dir)
        self.swarm_policies = POLICIES.read_text()
        self.swarm_policy_ids = re.findall(r'@id\("([^"]+)"\)', self.swarm_policies)
        self._lock = threading.RLock()

    def record(self, kind: str, **fields) -> dict:
        with self._lock:
            # Five runtimes write one run's records: the numbers come from one atomic counter (item seq "0000").
            self.seq = self._next_seq() - 1  # Checkpoint.record adds the 1
            return super().record(kind, **fields)

    def _next_seq(self) -> int:
        r = self.ddb.update_item(TableName=RECORDS_TABLE, Key={"run_id": {"S": self.run_id}, "seq": {"S": "0000"}},
                                 UpdateExpression="ADD next_seq :one", ExpressionAttributeValues={":one": {"N": "1"}},
                                 ReturnValues="UPDATED_NEW")
        return int(r["Attributes"]["next_seq"]["N"])

    def evaluate(self, principal: str, action: str, resource: str, context: dict, attrs: dict | None = None, **_) -> tuple:
        return cedar_decide(self.swarm_policies, self.swarm_policy_ids, principal, action, resource, context, attrs)


def _run_tool(cp, role: str, tools: dict, use: dict, context_for) -> str:
    """One tool call: Cedar decides, the tool runs, the result is recorded and screened before the model sees it."""
    name, args = use.get("name", ""), use.get("input") or {}
    if name not in tools:
        cp.record("tool_refused", who=role, tool=name, args=args, reason="not one of this agent's tools")
        result = {"error": f"{name!r} is not one of your tools"}
    else:
        d = cp.decide(f'Agent::"{role}"', name, NETWORK, context_for(name, args) if context_for else {})
        if d["decision"] != "Allow":
            result = {"error": "refused by policy: " + ", ".join(d["policies"])}
        else:
            try:
                result = tools[name](**args)
            except TypeError as e:
                result = {"error": f"bad arguments: {e}"}
            except Exception as e:  # a broken tool is an answer the agent can work with, not a crash
                result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    out = json.dumps(result, sort_keys=True, default=str)[:MAX_TOOL_RESULT]
    cp.record("tool_call", who=role, tool=name, tool_call_id=use.get("toolUseId", ""), args=args,
              result_sha256=sha256(out), result=out[:900])
    try:
        return cp.guard("INPUT", out, role)
    except Refused:
        return json.dumps({"error": "the guardrail blocked this tool result"})


def _valid_tool_names(message: dict, tools: dict) -> dict:
    """gpt-oss sometimes names a tool in a form Bedrock refuses to take back (e.g. 'functions.get_cell_state').
    Keep the conversation valid: use the known tool the name contains, or 'unknown_tool' (which the loop refuses)."""
    content = []
    for block in message["content"]:
        if "toolUse" in block and not re.fullmatch(r"[a-zA-Z0-9_-]+", block["toolUse"].get("name") or ""):
            parts = re.findall(r"[a-zA-Z0-9_-]+", block["toolUse"].get("name") or "")
            known = [p for p in parts if p in tools]
            block = {"toolUse": {**block["toolUse"], "name": known[-1] if known else "unknown_tool"}}
        content.append(block)
    return {"role": message["role"], "content": content}


def _with_text(message: dict, text: str) -> dict:
    """The assistant message with its text replaced by the guardrail's version, other blocks kept in order."""
    content, done = [], False
    for block in message["content"]:
        if "text" in block:
            if not done:
                content.append({"text": text})
                done = True
        else:
            content.append(block)
    return {"role": message["role"], "content": content}


def run_agent(cp, role: str, model_id: str, system: str, prompt: str, tools: dict, validate=None, context_for=None,
              max_turns: int = MAX_TURNS, max_tokens: int = 3000) -> dict:
    """One agent's turn, through the checkpoint. tools maps a tool name (contracts.TOOLS) to its function."""
    specs = [TOOLS[name] for name in tools]
    messages = [{"role": "user", "content": [{"text": cp.guard("INPUT", prompt, role)}]}]
    retried = False
    for turn in range(1, max_turns + 1):
        status = cp.model_status(model_id)  # read every turn: a pause takes effect mid-run
        d = cp.decide(f'Agent::"{role}"', "invoke_model", f'Model::"{model_id}"', {}, {"status": status})
        if d["decision"] != "Allow":
            cp.record("refused", who=role, model_id=model_id, reason=f"model is {status}")
            raise Refused(f"{model_id} is {status}: calls are refused")
        t0 = time.time()
        try:
            r = cp.runtime.converse(modelId=model_id, system=[{"text": system}], messages=messages,
                                    inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
                                    **({"toolConfig": {"tools": specs}} if specs else {}))
        except Exception as e:  # a failed call is a result too: record it before it propagates
            meta = getattr(e, "response", {}).get("ResponseMetadata", {}) if hasattr(e, "response") else {}
            cp.record("model_call_failed", who=role, model_id=model_id, turn=turn, request_id=meta.get("RequestId", ""),
                      latency_ms=int((time.time() - t0) * 1000), error=str(e)[:500])
            raise
        raw_names = [b["toolUse"].get("name", "") for b in r["output"]["message"]["content"] if "toolUse" in b]
        message = _valid_tool_names(r["output"]["message"], tools)
        text = "".join(block.get("text", "") for block in message["content"])
        uses = [block["toolUse"] for block in message["content"] if "toolUse" in block]
        if any(u["name"] != raw for u, raw in zip(uses, raw_names)):
            cp.record("tool_name_repaired", who=role, requested=raw_names, used=[u["name"] for u in uses])
        if text.strip():
            screened = cp.guard("OUTPUT", text, role)
            if screened != text:
                message, text = _with_text(message, screened), screened
        usage = r.get("usage", {})
        cp.record("model_call", who=role, model_id=model_id, turn=turn, request_id=r["ResponseMetadata"]["RequestId"],
                  latency_ms=int((time.time() - t0) * 1000), input_tokens=usage.get("inputTokens", 0),
                  output_tokens=usage.get("outputTokens", 0), stop_reason=r.get("stopReason", ""),
                  tool_calls=[u.get("name", "") for u in uses], prompt_sha256=sha256(system + "\n" + prompt),
                  output_sha256=sha256(text), output=text[:2000])
        messages.append(message)
        if uses:
            results = [{"toolResult": {"toolUseId": u["toolUseId"], "content": [{"text": _run_tool(cp, role, tools, u, context_for)}]}}
                       for u in uses]
            if turn == max_turns - 1:
                results.append({"text": "This is your last turn: reply now with the JSON answer only."})
            messages.append({"role": "user", "content": results})
            continue
        answer = parse_json(text)
        problems = ["the answer is not a JSON object"] if not answer else (validate(answer) if validate else [])
        if not problems:
            return answer
        if retried or turn == max_turns:
            cp.record("agent_failed", who=role, model_id=model_id, reason="; ".join(problems)[:500], output=text[:500])
            raise AgentFailed(f"{role}: {'; '.join(problems)[:200]}")
        retried = True  # one chance to fix the shape of the answer
        cp.record("contract_retry", who=role, problems=problems[:5])
        messages.append({"role": "user", "content": [{"text": "Your answer does not match the required JSON: "
                                                              + "; ".join(problems[:5]) + ". Reply again with JSON only."}]})
    cp.record("agent_failed", who=role, model_id=model_id, reason=f"no answer within {max_turns} turns")
    raise AgentFailed(f"{role}: no answer within {max_turns} turns")
