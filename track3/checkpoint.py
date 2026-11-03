"""The one checkpoint every model, tool and agent-to-agent call crosses.

For each call it: decides with Cedar, screens model input and output with
Amazon Bedrock Guardrails, and writes an audit record with the run id and trace id to
CloudWatch Logs (the official record), DynamoDB (for the judges' read-only live test) and a local
JSONL file (for the offline rebuild). Dynatrace gets a copy of each record as the operator view.
Each record is also a span in the run's trace (telemetry.py): CloudWatch always, Dynatrace and Arize for declared runs.
"""

import contextlib
import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags

import telemetry
from policy import PolicySet

REGION = "us-east-2"
LOG_GROUP = "/bb26/team04/track3"
RECORDS_TABLE = "bb26-team04-track3-records"
REGISTRY_TABLE = "bb26-team04-track3-registry"
GUARDRAIL_ID = "lr4zff1274hc"
GUARDRAIL_VERSION = "DRAFT"
HERE = Path(__file__).parent
PROMOTION_POLICIES = HERE / "policies" / "promotion.cedar"  # changes to the agents themselves
DT_LOGS = "https://yqs73977.sprint.apps.dynatracelabs.com/platform/classic/environment-api/v2/logs/ingest"
DT_TRACES = "https://yqs73977.sprint.dynatracelabs.com/api/v2/otlp/v1/traces"
# Spans go to the team's Arize space (agentic-aeric), where Arize Signal reviews the project and files issues.
# On AgentCore both ARIZE_SPACE_ID and ARIZE_API_KEY come from the team04/track3 secret.
ARIZE_SPACE_ID = os.environ.get("ARIZE_SPACE_ID", "U3BhY2U6NTE5NTA6ckhYRg==")
ARIZE_PROJECT = "bb26-hackathon"
ARIZE_KEY_FILE = Path.home() / ".arize_api_key_bb26"
ARIZE_TRACES = "https://otlp.arize.com/v1/traces"


def mirror_run(trace_id: str) -> None:
    """Dynatrace and Arize get this run's trace when their credentials are set now (dev runs remove them first)."""
    key = os.environ.get("ARIZE_API_KEY") or (ARIZE_KEY_FILE.read_text().strip() if ARIZE_KEY_FILE.exists() else "")
    if key:
        telemetry.mirror("arize", trace_id, lambda: telemetry.WithResource(OTLPSpanExporter(
            endpoint=ARIZE_TRACES, headers={"space_id": ARIZE_SPACE_ID, "api_key": key}), {"openinference.project.name": ARIZE_PROJECT}),
            keep=telemetry.is_openinference)
    token = os.environ.get("DT_TRACES_TOKEN")
    if token:  # a classic access token (dt0c01.) is sent as Api-Token, a platform token as Bearer
        auth = f"Api-Token {token}" if token.startswith("dt0c01.") else f"Bearer {token}"
        telemetry.mirror("dynatrace", trace_id, lambda: OTLPSpanExporter(endpoint=DT_TRACES, headers={"Authorization": auth}))


def _root_parent(trace_id: str, parent_span_id: str) -> tuple:
    """Where a runtime's root span hangs, and how that was learned.
    - "traceparent": the caller sent the W3C traceparent header (InvokeAgentRuntime traceParent) and this runtime's
      instrumentation made it the current context, so the root continues the caller's trace the standard way;
    - "payload": the span id the caller put in the payload (the fallback when the header was not picked up);
    - "none": the run's first runtime starts the trace."""
    current = trace.get_current_span().get_span_context()
    if current.is_valid and current.trace_id == int(trace_id, 16):
        return otel_context.get_current(), "traceparent"
    if not parent_span_id:
        return otel_context.Context(), "none"
    parent = SpanContext(int(trace_id, 16), int(parent_span_id, 16), is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
    return trace.set_span_in_context(NonRecordingSpan(parent), otel_context.Context()), "payload"


class Refused(Exception):
    """A call the checkpoint did not allow."""


MAX_SPAN_TEXT = 20000  # input.value / output.value are cut here, so one span stays well under OTLP limits


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Checkpoint:
    def __init__(self, run_id: str, trace_id: str, out_dir: Path, parent_span_id: str = ""):
        self.run_id, self.trace_id = run_id, trace_id
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.local = self.out_dir / "records.jsonl"
        self.seq = 0
        self.logs = boto3.client("logs", region_name=REGION)
        self.ddb = boto3.client("dynamodb", region_name=REGION)
        self.runtime = boto3.client("bedrock-runtime", region_name=REGION)
        mirror_run(trace_id)
        self.tracer = telemetry.tracer(trace_id)
        parent_context, parent_from = _root_parent(trace_id, parent_span_id)
        self.root = self.tracer.start_span(f"track3 {run_id}", context=parent_context,
                                           attributes={"openinference.span.kind": "AGENT", "track3.run_id": run_id,
                                                                       "track3.parent_from": parent_from})
        self._root_open = True
        # The base checkpoint holds the promotion set. SwarmCheckpoint decides network changes with swarm.cedar instead.
        self.policy_sets = {"promotion": PolicySet(PROMOTION_POLICIES)}
        try:
            with telemetry.quiet():
                self.logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName=run_id)
        except self.logs.exceptions.ResourceAlreadyExistsException:
            pass

    # ---- trace -------------------------------------------------------------------------------
    @contextlib.contextmanager
    def active(self):
        """Makes the run's root the current span, so spans ADOT starts inside (Bedrock, boto3) join the run's trace."""
        token = otel_context.attach(trace.set_span_in_context(self.root))
        try:
            yield self
        finally:
            otel_context.detach(token)

    @contextlib.contextmanager
    def context(self):
        """Makes the step that is running now (or the run's root) the current span, for code that starts spans itself."""
        token = otel_context.attach(self._parent())
        try:
            yield
        finally:
            otel_context.detach(token)

    def _parent(self):
        """The current span when it belongs to this run's trace, so a record nests under the step that wrote it;
        otherwise the run's root."""
        current = trace.get_current_span()
        if current.is_recording() and current.get_span_context().trace_id == int(self.trace_id, 16):
            return otel_context.get_current()
        return trace.set_span_in_context(self.root)

    @contextlib.contextmanager
    def span(self, name: str, kind: str, **attrs):
        """A real span around one step (an agent's turn, a model call, a tool call): it has a duration, nests under the
        step that started it, and is OK unless the step raised (then ERROR, with the exception). kind is OpenInference's
        span kind (AGENT, LLM, TOOL, CHAIN), which Arize reads."""
        attributes = {"openinference.span.kind": kind, "track3.run_id": self.run_id,
                      **{k: v for k, v in attrs.items() if v is not None}}
        with self.tracer.start_as_current_span(name, context=self._parent(), attributes=attributes) as s:
            yield s
            s.set_status(Status(StatusCode.OK))

    def close(self) -> None:
        """Ends the run's root span once and sends what is buffered. Every runtime calls it when its part is done."""
        if self._root_open:
            self._root_open = False
            self.root.end()
        telemetry.flush()

    # ---- audit -------------------------------------------------------------------------------
    def record(self, kind: str, **fields) -> dict:
        self.seq += 1
        rec = {"run_id": self.run_id, "trace_id": self.trace_id, "seq": self.seq, "time": now_iso(), "kind": kind, **fields}
        line = json.dumps(rec, sort_keys=True)
        with telemetry.quiet():  # the record itself is a span (_to_trace); its writes are bookkeeping
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
        if rec["kind"] in ("model_call", "tool_call"):
            return  # these already have a real span (Checkpoint.span); the record stays in the audit trail
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
        span = self.tracer.start_span(name, context=self._parent(), attributes=attrs)
        span.set_status(Status(StatusCode.OK))
        span.end()
        if rec["kind"] == "run_end":
            self.close()

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
                 policy_set: str = "") -> tuple:
        """Ask Cedar without writing a record (used to check whether an approval would help). SwarmCheckpoint overrides
        this and ignores policy_set; the base checkpoint needs one, so a missing name is an error, not a default."""
        if policy_set not in self.policy_sets:
            raise KeyError(f"unknown policy set {policy_set!r}; this checkpoint holds {sorted(self.policy_sets)}")
        return self.policy_sets[policy_set].evaluate(principal, action, resource, context, attrs)

    def decide(self, principal: str, action: str, resource: str, context: dict, attrs: dict | None = None,
               policy_set: str = "") -> dict:
        decision, why, errors = self.evaluate(principal, action, resource, context, attrs, policy_set=policy_set)
        # Swarm decisions keep their original record shape; a named set says which one decided.
        extra = {"policy_set": policy_set} if policy_set else {}
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
        """One model call with no tools (the judge), on Strands through the checkpoint:
        policy and model registry, guardrail in, Bedrock, guardrail out, audit (agent_loop.py)."""
        from agent_loop import ask, make_agent  # here, not at import: agent_loop imports this module

        agent, governed = make_agent(self, agent, model_id, system, prompt, {}, {}, max_turns=1, max_tokens=max_tokens)
        text = ask(agent, self, self.guard("INPUT", prompt, agent.name))
        return {"text": text, "usage": governed.usage}

    def handoff(self, from_agent: str, to_agent: str, summary: str) -> dict:
        d = self.decide(f'Agent::"{from_agent}"', "handoff", f'Agent::"{to_agent}"', {})
        if d["decision"] != "Allow":
            raise Refused(f"{from_agent} may not hand work to {to_agent}")
        return self.record("handoff", from_agent=from_agent, to_agent=to_agent, summary=summary[:500])
