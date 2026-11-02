"""The one checkpoint every model, tool and agent-to-agent call crosses.

For each call it: decides with Cedar (policies/track3.cedar), screens model input and output with
Amazon Bedrock Guardrails, and writes an audit record with the run id and trace id to
CloudWatch Logs (the official record), DynamoDB (for the judges' read-only live test) and a local
JSONL file (for the offline rebuild). Dynatrace gets a copy of each record as the operator view.
"""

import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

from policy import PolicySet

REGION = "us-east-2"
LOG_GROUP = "/bb26/team04/track3"
RECORDS_TABLE = "bb26-team04-track3-records"
REGISTRY_TABLE = "bb26-team04-track3-registry"
GUARDRAIL_ID = "lr4zff1274hc"
GUARDRAIL_VERSION = "DRAFT"
HERE = Path(__file__).parent
POLICIES = HERE / "policies" / "track3.cedar"  # network changes; frozen, its hash is in every declared run
PROMOTION_POLICIES = HERE / "policies" / "promotion.cedar"  # changes to the agents themselves
DT_LOGS = "https://yqs73977.sprint.apps.dynatracelabs.com/platform/classic/environment-api/v2/logs/ingest"
# Spans go to the team's Arize space (agentic-aeric), where Arize Signal reviews the project and files issues.
# On AgentCore both ARIZE_SPACE_ID and ARIZE_API_KEY come from the team04/track3 secret.
ARIZE_SPACE_ID = os.environ.get("ARIZE_SPACE_ID", "U3BhY2U6NTE5NTA6ckhYRg==")
ARIZE_PROJECT = "bb26-hackathon"
ARIZE_KEY_FILE = Path.home() / ".arize_api_key_bb26"

# Tools that change the network. Each needs a policy decision and, where the policy says so, a person's approval.
STATE_CHANGING = {"restart_all_towers", "set_tower_power", "rollback_model"}


class _RunTraceIds(RandomIdGenerator):
    """Every span of a run uses the run's trace id, so Arize (and Dynatrace) show the same id as the audit records."""

    def __init__(self, trace_id: str):
        self._trace_id = int(trace_id, 16)

    def generate_trace_id(self) -> int:
        return self._trace_id


def make_tracer(trace_id: str):
    provider = TracerProvider(resource=Resource.create({"service.name": "track3-checkpoint", "openinference.project.name": ARIZE_PROJECT}),
                              id_generator=_RunTraceIds(trace_id))
    key = os.environ.get("ARIZE_API_KEY") or (ARIZE_KEY_FILE.read_text().strip() if ARIZE_KEY_FILE.exists() else "")
    if key:
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
            endpoint="https://otlp.arize.com/v1/traces", headers={"space_id": ARIZE_SPACE_ID, "api_key": key})))
    token = os.environ.get("DT_TRACES_TOKEN")
    if token:
        provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
            endpoint="https://yqs73977.sprint.dynatracelabs.com/api/v2/otlp/v1/traces", headers={"Authorization": f"Bearer {token}"})))
    return provider, provider.get_tracer("track3")


class Refused(Exception):
    """A call the checkpoint did not allow."""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Checkpoint:
    def __init__(self, run_id: str, trace_id: str, out_dir: Path):
        self.run_id, self.trace_id = run_id, trace_id
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.local = self.out_dir / "records.jsonl"
        self.seq = 0
        self.logs = boto3.client("logs", region_name=REGION)
        self.ddb = boto3.client("dynamodb", region_name=REGION)
        self.runtime = boto3.client("bedrock-runtime", region_name=REGION)
        self.provider, self.tracer = make_tracer(trace_id)
        self.root = self.tracer.start_span(f"track3 {run_id}", attributes={"openinference.span.kind": "AGENT", "track3.run_id": run_id})
        self.policy_sets = {"network": PolicySet(POLICIES), "promotion": PolicySet(PROMOTION_POLICIES)}
        self.policies, self.policy_ids = self.policy_sets["network"].text, self.policy_sets["network"].ids
        try:
            self.logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName=run_id)
        except self.logs.exceptions.ResourceAlreadyExistsException:
            pass

    # ---- audit -------------------------------------------------------------------------------
    def record(self, kind: str, **fields) -> dict:
        self.seq += 1
        rec = {"run_id": self.run_id, "trace_id": self.trace_id, "seq": self.seq, "time": now_iso(), "kind": kind, **fields}
        line = json.dumps(rec, sort_keys=True)
        self.logs.put_log_events(logGroupName=LOG_GROUP, logStreamName=self.run_id,
                                 logEvents=[{"timestamp": int(time.time() * 1000), "message": line}])
        self.ddb.put_item(TableName=RECORDS_TABLE, Item={
            "run_id": {"S": self.run_id}, "seq": {"S": f"{self.seq:04d}"}, "trace_id": {"S": self.trace_id},
            "kind": {"S": kind}, "time": {"S": rec["time"]}, "record": {"S": line}})
        with self.local.open("a") as f:
            f.write(line + "\n")
        self._to_dynatrace(rec)
        self._to_trace(rec)
        return rec

    def _to_trace(self, rec: dict) -> None:
        kinds = {"model_call": "LLM", "judged": "EVALUATOR", "decision": "GUARDRAIL", "guardrail": "GUARDRAIL", "executed": "TOOL",
                 "denied": "TOOL", "handoff": "AGENT"}
        attrs = {"openinference.span.kind": kinds.get(rec["kind"], "CHAIN"), "track3.run_id": rec["run_id"], "track3.seq": rec["seq"]}
        for k in ("action", "decision", "tool", "who", "approver", "model_id", "verdict", "score", "reason"):
            if rec.get(k) not in (None, "", []):
                attrs[f"track3.{k}"] = str(rec[k])
        if rec.get("policies"):
            attrs["track3.policies"] = ", ".join(rec["policies"])
        if rec["kind"] == "model_call":
            attrs.update({"llm.model_name": rec["model_id"], "output.value": rec.get("output", ""),
                          "llm.token_count.prompt": rec.get("input_tokens", 0), "llm.token_count.completion": rec.get("output_tokens", 0)})
        name = f"{rec['kind']} {rec.get('action') or rec.get('tool') or rec.get('who') or ''}".strip()
        span = self.tracer.start_span(name, context=trace.set_span_in_context(self.root), attributes=attrs)
        span.end()
        if rec["kind"] == "run_end":
            self.root.end()
            self.provider.force_flush()

    def _to_dynatrace(self, rec: dict) -> None:
        token = os.environ.get("DT_PLATFORM_TOKEN")
        if not token:
            return
        body = {"timestamp": rec["time"], "service.name": "track3-checkpoint", "log.source": "breaking-barriers/track3",
                "loglevel": "WARN" if rec.get("decision") == "Deny" else "INFO",
                "content": f"{rec['kind']}: " + json.dumps({k: v for k, v in rec.items() if k not in ("run_id", "trace_id", "time")})[:900],
                "trace_id": rec["trace_id"], "track3.run_id": rec["run_id"], "track3.seq": rec["seq"], "track3.kind": rec["kind"],
                "track3.decision": rec.get("decision", ""), "track3.action": rec.get("action", "")}
        req = urllib.request.Request(DT_LOGS, data=json.dumps([body]).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:  # the operator view must never block the official record
            pass

    # ---- policy ------------------------------------------------------------------------------
    def evaluate(self, principal: str, action: str, resource: str, context: dict, attrs: dict | None = None,
                 policy_set: str = "network") -> tuple:
        """Ask Cedar without writing a record (used to check whether an approval would help)."""
        return self.policy_sets[policy_set].evaluate(principal, action, resource, context, attrs)

    def decide(self, principal: str, action: str, resource: str, context: dict, attrs: dict | None = None,
               policy_set: str = "network") -> dict:
        decision, why, errors = self.evaluate(principal, action, resource, context, attrs, policy_set)
        # Network decisions keep their original record shape; other sets say which one decided.
        extra = {} if policy_set == "network" else {"policy_set": policy_set}
        return self.record("decision", principal=principal, action=action, resource=resource, context=context,
                           resource_attrs=attrs or {}, decision=decision, policies=why, errors=errors, **extra)

    # ---- guardrails --------------------------------------------------------------------------
    def guard(self, source: str, text: str, who: str) -> str:
        r = self.runtime.apply_guardrail(guardrailIdentifier=GUARDRAIL_ID, guardrailVersion=GUARDRAIL_VERSION,
                                         source=source, content=[{"text": {"text": text[:20000]}}])
        intervened = r["action"] == "GUARDRAIL_INTERVENED"
        self.record("guardrail", who=who, source=source, action_taken=r["action"], text_sha256=sha256(text),
                    assessments=json.loads(json.dumps(r.get("assessments", []), default=str))[:3] if intervened else [])
        if intervened and source == "INPUT":
            raise Refused(f"guardrail blocked the input to {who}")
        return r["outputs"][0]["text"] if intervened and r.get("outputs") else text

    # ---- model registry ----------------------------------------------------------------------
    def model_status(self, model_id: str) -> str:
        item = self.ddb.get_item(TableName=REGISTRY_TABLE, Key={"model_id": {"S": model_id}}).get("Item")
        return item["status"]["S"] if item else "unregistered"

    def set_model_status(self, model_id: str, status: str, who: str) -> dict:
        self.ddb.put_item(TableName=REGISTRY_TABLE, Item={"model_id": {"S": model_id}, "status": {"S": status},
                                                           "changed_by": {"S": who}, "changed_at": {"S": now_iso()}})
        return self.record("registry", model_id=model_id, status=status, changed_by=who)

    # ---- calls -------------------------------------------------------------------------------
    def call_model(self, agent: str, model_id: str, system: str, prompt: str, max_tokens: int = 1500) -> dict:
        """One model call, through policy, guardrail in, Bedrock, guardrail out, audit."""
        status = self.model_status(model_id)
        d = self.decide(f'Agent::"{agent}"', "invoke_model", f'Model::"{model_id}"', {}, {"status": status})
        if d["decision"] != "Allow":
            self.record("refused", who=agent, model_id=model_id, reason=f"model is {status}")
            raise Refused(f"{model_id} is {status}: calls are refused")
        prompt = self.guard("INPUT", prompt, agent)
        t0 = time.time()
        try:
            r = self.runtime.converse(modelId=model_id, system=[{"text": system}],
                                      messages=[{"role": "user", "content": [{"text": prompt}]}],
                                      inferenceConfig={"maxTokens": max_tokens, **({} if "anthropic" in model_id else {"temperature": 0.2})})
        except Exception as e:  # a failed call is a result too: record it before it propagates
            meta = getattr(e, "response", {}).get("ResponseMetadata", {}) if hasattr(e, "response") else {}
            self.record("model_call_failed", who=agent, model_id=model_id, request_id=meta.get("RequestId", ""),
                        latency_ms=int((time.time() - t0) * 1000), error=str(e)[:500], prompt_sha256=sha256(system + "\n" + prompt))
            raise
        text = "".join(c.get("text", "") for c in r["output"]["message"]["content"])
        text = self.guard("OUTPUT", text, agent)
        usage = r.get("usage", {})
        self.record("model_call", who=agent, model_id=model_id, request_id=r["ResponseMetadata"]["RequestId"],
                    latency_ms=int((time.time() - t0) * 1000), input_tokens=usage.get("inputTokens", 0),
                    output_tokens=usage.get("outputTokens", 0), prompt_sha256=sha256(system + "\n" + prompt),
                    output_sha256=sha256(text), output=text[:2000])
        return {"text": text, "usage": usage}

    def handoff(self, from_agent: str, to_agent: str, summary: str) -> dict:
        d = self.decide(f'Agent::"{from_agent}"', "handoff", f'Agent::"{to_agent}"', {})
        if d["decision"] != "Allow":
            raise Refused(f"{from_agent} may not hand work to {to_agent}")
        return self.record("handoff", from_agent=from_agent, to_agent=to_agent, summary=summary[:500])

    def _execute(self, agent: str, tool: str, args: dict, execute, **extra) -> dict:
        """Runs the tool. In the cloud `execute` goes through the AgentCore Gateway, whose own Cedar can still refuse."""
        out = execute(tool, args)
        if isinstance(out, dict) and out.get("allowed") is False:
            return self.record("denied", who=agent, tool=tool, args=args, policies=[f"agentcore-policy: {out.get('text', '')[:200]}"], **extra)
        return self.record("executed", who=agent, tool=tool, args=args, result=out.get("text") if isinstance(out, dict) else out, **extra)

    def second_wall(self, agent: str, tool: str, args: dict, call) -> dict:
        """Asks AWS's own Cedar at the AgentCore Gateway about a tool this checkpoint already denied: it must refuse too."""
        out = call(tool, args)
        return self.record("gateway_check", who=agent, tool=tool, allowed=out["allowed"], reason=out["text"][:300])

    def tool(self, agent: str, tool: str, args: dict, approve, execute) -> dict:
        """A tool call. State-changing tools ask a person only when the policy would allow the call with approval."""
        resource = 'Network::"mobile-env-medium"'
        context = {"human_approved": False, "towers": int(args.get("towers", 1 if tool == "set_tower_power" else 0))}
        first = self.decide(f'Agent::"{agent}"', tool, resource, context)
        if first["decision"] == "Allow":
            return self._execute(agent, tool, args, execute)
        if tool not in STATE_CHANGING or "no policy permits this call" not in first["policies"]:
            return self.record("denied", who=agent, tool=tool, args=args, policies=first["policies"])
        # Would a person's approval make it allowed? Ask Cedar before asking the person.
        if self.evaluate(f'Agent::"{agent}"', tool, resource, {**context, "human_approved": True})[0] != "Allow":
            return self.record("denied", who=agent, tool=tool, args=args, policies=["not allowed even with approval"])
        self.record("approval_requested", who=agent, tool=tool, args=args)
        approver, ok, note = approve(tool, args)
        self.record("approval", tool=tool, approver=approver, approved=ok, note=note)
        if not ok:
            return self.record("denied", who=agent, tool=tool, args=args, policies=["person declined"])
        final = self.decide(f'Agent::"{agent}"', tool, resource, {**context, "human_approved": True})
        if final["decision"] != "Allow":
            return self.record("denied", who=agent, tool=tool, args=args, policies=final["policies"])
        return self._execute(agent, tool, args, execute, approver=approver)
