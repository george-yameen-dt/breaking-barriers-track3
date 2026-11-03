"""Where the run's spans go: one tracer provider (ADOT's on AgentCore), exporting to CloudWatch, Dynatrace and Arize.

Offline. An in-memory exporter on the global provider stands in for ADOT's CloudWatch exporter; fake OTLP exporters
stand in for Dynatrace and Arize.
"""

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import checkpoint

TRACE = "0" * 31 + "1"  # conftest's offline_cp run
PARENT = "00f067aa0ba902b7"


@pytest.fixture(scope="session")
def _global_memory():
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    return memory


@pytest.fixture
def spans(_global_memory):
    """Every span the global provider finished during the test (what ADOT would send to CloudWatch)."""
    _global_memory.clear()
    return _global_memory


def finished(spans, name):
    return [s for s in spans.get_finished_spans() if s.name == name]


class FakeOTLP(SpanExporter):
    made = []

    def __init__(self, endpoint, headers):
        self.endpoint, self.headers, self.spans = endpoint, headers, []
        FakeOTLP.made.append(self)

    def export(self, spans):
        self.spans += list(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.fixture
def fake_mirrors(monkeypatch, tmp_path):
    """Fake Dynatrace and Arize exporters, and no credentials until a test sets them."""
    import telemetry

    FakeOTLP.made = []
    for key in ("ARIZE_API_KEY", "DT_TRACES_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(checkpoint, "ARIZE_KEY_FILE", tmp_path / "no-arize-key")
    monkeypatch.setattr(checkpoint, "OTLPSpanExporter", FakeOTLP)
    monkeypatch.setattr(telemetry, "_MIRRORS", {})
    monkeypatch.setattr(checkpoint.boto3, "client", lambda *a, **k: MagicMock())
    return FakeOTLP.made


# ---- the checkpoint's spans join the global provider --------------------------------------------
def test_checkpoint_spans_go_through_the_global_provider(spans, offline_cp):
    offline_cp.record("decision", action="read_network", decision="Allow")
    assert finished(spans, "decision read_network"), [s.name for s in spans.get_finished_spans()]


def test_record_spans_carry_the_run_trace_id_under_the_root(spans, offline_cp):
    offline_cp.record("decision", action="read_network", decision="Allow")
    offline_cp.record("run_end", outcome="ok")
    root, rec = finished(spans, "track3 test-run")[0], finished(spans, "decision read_network")[0]
    assert root.parent is None and root.context.trace_id == int(TRACE, 16)
    assert rec.context.trace_id == root.context.trace_id and rec.parent.span_id == root.context.span_id


def test_two_runs_in_one_process_keep_their_own_trace_ids(spans, offline_cp, tmp_path):
    other = checkpoint.Checkpoint("test-run-2", "ab" * 16, tmp_path / "run2")
    offline_cp.record("run_end")
    other.record("run_end")
    roots = {s.name: s.context.trace_id for s in spans.get_finished_spans() if s.parent is None}
    assert roots == {"track3 test-run": int(TRACE, 16), "track3 test-run-2": int("ab" * 16, 16)}


def test_spans_started_while_active_join_the_run_trace(spans, offline_cp):
    """ADOT starts its Bedrock and boto3 spans from the current context; inside active() that is the run's root."""
    with offline_cp.active():
        trace.get_tracer("opentelemetry.instrumentation.botocore").start_span("chat openai.gpt-oss-120b-1:0").end()
    chat = finished(spans, "chat openai.gpt-oss-120b-1:0")[0]
    assert chat.context.trace_id == int(TRACE, 16)
    assert chat.parent.span_id == offline_cp.root.get_span_context().span_id


# ---- one trace across the five runtimes -----------------------------------------------------------
def test_a_child_runtime_root_hangs_under_the_orchestrator_root(spans, offline_cp, tmp_path):
    child = checkpoint.Checkpoint("test-run", TRACE, tmp_path / "coverage", parent_span_id=PARENT)
    child.close()
    root = finished(spans, "track3 test-run")[0]
    assert root.context.trace_id == int(TRACE, 16) and root.parent.span_id == int(PARENT, 16)


def test_close_ends_the_root_once(spans, offline_cp):
    offline_cp.close()
    offline_cp.close()
    assert len(finished(spans, "track3 test-run")) == 1


def test_run_end_then_close_ends_the_root_once(spans, offline_cp):
    offline_cp.record("run_end")
    offline_cp.close()
    assert len(finished(spans, "track3 test-run")) == 1


# ---- mirrors: Dynatrace and Arize get only the runs that asked ------------------------------------
def test_a_mirror_exports_only_the_traces_it_was_given(spans):
    import telemetry

    memory = InMemorySpanExporter()
    telemetry.mirror("test-only-a", "a" * 32, lambda: memory)
    telemetry.tracer("a" * 32).start_span("in a").end()
    telemetry.tracer("b" * 32).start_span("in b").end()
    telemetry.flush()
    assert [s.name for s in memory.get_finished_spans()] == ["in a"]


def test_a_mirror_is_built_once_per_destination(spans):
    import telemetry

    built = []

    def make():
        built.append(1)
        return InMemorySpanExporter()

    telemetry.mirror("test-once", "c" * 32, make)
    telemetry.mirror("test-once", "d" * 32, make)
    assert len(built) == 1


def test_a_declared_run_is_mirrored_to_arize_and_dynatrace(spans, fake_mirrors, monkeypatch, tmp_path):
    monkeypatch.setenv("ARIZE_API_KEY", "arize-key")
    monkeypatch.setenv("DT_TRACES_TOKEN", "dt0c01.ABC.DEF")
    checkpoint.Checkpoint("mirrored", "e" * 32, tmp_path).record("run_end")
    by_endpoint = {f.endpoint: f for f in fake_mirrors}
    assert set(by_endpoint) == {checkpoint.ARIZE_TRACES, checkpoint.DT_TRACES}
    for f in fake_mirrors:
        assert "track3 mirrored" in [s.name for s in f.spans], f.endpoint
    assert by_endpoint[checkpoint.DT_TRACES].headers["Authorization"] == "Api-Token dt0c01.ABC.DEF"
    assert by_endpoint[checkpoint.ARIZE_TRACES].headers["api_key"] == "arize-key"


def test_arize_spans_carry_the_arize_project(spans, fake_mirrors, monkeypatch, tmp_path):
    monkeypatch.setenv("ARIZE_API_KEY", "arize-key")
    checkpoint.Checkpoint("arize-project", "f" * 32, tmp_path).record("run_end")
    arize = next(f for f in fake_mirrors if f.endpoint == checkpoint.ARIZE_TRACES)
    assert {s.resource.attributes.get("openinference.project.name") for s in arize.spans} == {checkpoint.ARIZE_PROJECT}


def test_a_platform_token_is_sent_as_bearer(spans, fake_mirrors, monkeypatch, tmp_path):
    monkeypatch.setenv("DT_TRACES_TOKEN", "dt0s16.ABC.DEF")
    checkpoint.Checkpoint("bearer", "1" * 32, tmp_path).record("run_end")
    dt = next(f for f in fake_mirrors if f.endpoint == checkpoint.DT_TRACES)
    assert dt.headers["Authorization"] == "Bearer dt0s16.ABC.DEF"


def test_dev_run_exports_to_all_destinations(spans, fake_mirrors, monkeypatch, tmp_path):
    """Every run is traced, whether or not it was declared: both runs land in both mirrors."""
    monkeypatch.setenv("ARIZE_API_KEY", "arize-key")
    monkeypatch.setenv("DT_TRACES_TOKEN", "dt0c01.ABC.DEF")
    checkpoint.Checkpoint("declared", "2" * 32, tmp_path / "declared").record("run_end")
    checkpoint.Checkpoint("dev", "3" * 32, tmp_path / "dev").record("run_end")
    for f in fake_mirrors:
        assert {s.context.trace_id for s in f.spans} == {int("2" * 32, 16), int("3" * 32, 16)}, f.endpoint


def test_service_name_is_multi_agent_swarm(monkeypatch):
    import telemetry
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    assert telemetry.default_resource().attributes["service.name"] == "multi-agent-swarm"
    monkeypatch.setenv("OTEL_SERVICE_NAME", "something-else")
    assert telemetry.default_resource().attributes["service.name"] == "something-else"


def test_missing_arize_key_fails_loudly(spans, fake_mirrors, monkeypatch, tmp_path):
    monkeypatch.setattr(checkpoint, "TRACE_EXPORT_REQUIRED", True)
    monkeypatch.setenv("DT_TRACES_TOKEN", "dt0c01.ABC.DEF")
    with pytest.raises(checkpoint.Refused, match="arize"):
        checkpoint.Checkpoint("no-arize", "4" * 32, tmp_path)
    rec = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert rec[-1]["kind"] == "trace_export_unavailable" and rec[-1]["missing"] == ["arize"]


def test_missing_dynatrace_token_fails_loudly(spans, fake_mirrors, monkeypatch, tmp_path):
    monkeypatch.setattr(checkpoint, "TRACE_EXPORT_REQUIRED", True)
    monkeypatch.setenv("ARIZE_API_KEY", "arize-key")
    with pytest.raises(checkpoint.Refused, match="dynatrace"):
        checkpoint.Checkpoint("no-dt", "5" * 32, tmp_path)
    rec = [json.loads(line) for line in (tmp_path / "records.jsonl").read_text().splitlines()]
    assert rec[-1]["kind"] == "trace_export_unavailable" and rec[-1]["missing"] == ["dynatrace"]


def test_cloud_handle_keeps_the_credentials_of_a_dev_run(monkeypatch):
    from agents import cloud
    monkeypatch.setenv("DT_TRACES_TOKEN", "dt0c01.keep")
    monkeypatch.setenv("ARIZE_API_KEY", "keep")
    monkeypatch.setattr(cloud, "SwarmCheckpoint", MagicMock())
    monkeypatch.setattr(cloud, "_handle", lambda *a, **k: {})
    cloud.handle({"mode": "swarm", "run_id": "r", "trace_id": "6" * 32, "declared": False})
    assert os.environ["DT_TRACES_TOKEN"] == "dt0c01.keep" and os.environ["ARIZE_API_KEY"] == "keep"


def test_the_laptop_dev_run_does_not_strip_credentials():
    import inspect
    import swarm
    assert "environ.pop" not in inspect.getsource(swarm.cmd_run) and "ARIZE_KEY_FILE" not in inspect.getsource(swarm.cmd_run)


def test_a_record_span_carries_the_agent_name(spans, offline_cp):
    offline_cp.record("decision", who="coverage", action="propose", decision="Allow", policies=["domain-ownership"])
    s = next(s for s in spans.get_finished_spans() if s.name.startswith("decision propose"))
    assert s.attributes["gen_ai.agent.name"] == "coverage"
    assert offline_cp.root.attributes["gen_ai.agent.name"] == os.environ.get("AGENT_ROLE", "orchestrator")


# ---- the checkpoint's own bookkeeping is not traced -----------------------------------------------
def test_audit_writes_are_not_traced(offline_cp):
    utils = pytest.importorskip("opentelemetry.instrumentation.utils")
    seen = []
    offline_cp.logs.put_log_events.side_effect = lambda **k: seen.append(utils.is_instrumentation_enabled())
    offline_cp.ddb.put_item.side_effect = lambda **k: seen.append(utils.is_instrumentation_enabled())
    offline_cp.record("decision", action="read_network", decision="Allow")
    assert seen == [False, False]


def test_the_swarm_record_counter_is_not_traced(offline_cp, tmp_path):
    utils = pytest.importorskip("opentelemetry.instrumentation.utils")
    from agents.governance import SwarmCheckpoint

    cp = SwarmCheckpoint("swarm-run", TRACE, tmp_path / "swarm")
    seen = []

    def counter(**_):
        seen.append(utils.is_instrumentation_enabled())
        return {"Attributes": {"next_seq": {"N": "1"}}}

    cp.ddb.update_item.side_effect = counter
    cp.record("decision", action="read_network", decision="Allow")
    assert seen == [False]


# ---- the swarm passes its trace context on ---------------------------------------------------------
def test_specialists_run_in_the_orchestrators_trace_context(spans):
    from agents import orchestrator

    seen = {}

    class FakeRun:
        def __init__(self):
            self.reports, self.cp = {}, MagicMock()

        def call_specialist(self, _run, role):
            seen[role] = trace.get_current_span().get_span_context().span_id
            return {"domain_verdict": "no_issue", "findings": [], "proposals": []}

    with trace.get_tracer("test").start_as_current_span("orchestrator") as span:
        orchestrator.gather_reports(FakeRun(), lambda *_: None)
    assert set(seen.values()) == {span.get_span_context().span_id}, seen


def _cloud_payload(mode: str, **extra) -> dict:
    return {"mode": mode, "run_id": "cloud-run", "trace_id": TRACE, "fault": "outage-C",
            "taken_at": "2026-11-02T12:00:00.000+00:00", "declared": False, **extra}


def test_the_orchestrator_hands_its_root_span_to_the_other_runtimes(spans, offline_cp, monkeypatch):
    from agents import cloud

    seen = {}

    def fake_run_incident(cp, _net, use_dynatrace=False, pause_operator="", remote=None, alert=""):
        seen["parent_span_id"] = remote.base["parent_span_id"]
        seen["root"] = format(cp.root.get_span_context().span_id, "016x")
        seen["current"] = format(trace.get_current_span().get_span_context().span_id, "016x")
        return {"outcome": "ready to apply"}

    monkeypatch.setattr(cloud, "run_incident", fake_run_incident)
    monkeypatch.setattr(cloud.SwarmCheckpoint, "_next_seq", lambda self: self.seq + 1)
    monkeypatch.setattr(cloud.SwarmCheckpoint, "policy_pin", lambda self, name: None)  # nothing pinned yet
    cloud.handle(_cloud_payload("swarm"))
    assert seen["parent_span_id"] == seen["root"] == seen["current"], seen


def test_a_critic_runtime_closes_its_root_under_the_orchestrator(spans, offline_cp, monkeypatch):
    from agents import cloud

    monkeypatch.setattr(cloud, "assess_proposals", lambda run: [])
    monkeypatch.setattr(cloud.SwarmCheckpoint, "_next_seq", lambda self: self.seq + 1)
    out = cloud.handle(_cloud_payload("critic", role="risk", reports={}, proposals=[], checks={}, parent_span_id=PARENT))
    assert out == {"assessments": [], "critic_checked": []}, out
    (root,) = finished(spans, "track3 cloud-run")
    assert root.parent.span_id == int(PARENT, 16) and root.context.trace_id == int(TRACE, 16)


# ---- the agents run on Strands: it traces each turn, model call and tool call; the checkpoint still decides each one ---
def _reply(content, stop="end_turn"):
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop, "metrics": {"latencyMs": 5},
            "usage": {"inputTokens": 11, "outputTokens": 7, "totalTokens": 18}, "ResponseMetadata": {"RequestId": "req-1"}}


@pytest.fixture
def bedrock(monkeypatch):
    """A fake Bedrock Runtime client under Strands' BedrockModel; set .converse.side_effect to script the model."""
    import agent_loop

    client = MagicMock()
    session = MagicMock(region_name="us-east-2")
    session.client.return_value = client
    monkeypatch.setattr(agent_loop.boto3, "Session", lambda **kw: session)
    return SimpleNamespace(converse=client.converse)  # the checkpoint wraps client.converse; keep the mock itself


@pytest.fixture
def agent_cp(offline_cp, monkeypatch):
    """The offline checkpoint with every model approved, every call allowed and the guardrail passing text through."""
    monkeypatch.setattr(offline_cp, "model_status", lambda model_id: "approved")
    monkeypatch.setattr(offline_cp, "evaluate", lambda *a, **k: ("Allow", ["test-allow"], []))
    offline_cp.runtime.apply_guardrail.return_value = {"action": "NONE"}
    return offline_cp


def _records(cp):
    return [json.loads(line) for line in cp.local.read_text().splitlines()]


def _run_coverage(cp, tools=None):
    from agents.governance import run_agent

    return run_agent(cp, "coverage", "openai.gpt-oss-120b-1:0", "system", "find the fault",
                     tools if tools is not None else {"get_cell_state": lambda cell_id: {"cell_id": cell_id, "enabled": 0}})


def test_strands_traces_an_agent_turn_inside_the_run_trace(spans, agent_cp, bedrock):
    from opentelemetry.trace import StatusCode

    bedrock.converse.side_effect = [
        _reply([{"toolUse": {"toolUseId": "t1", "name": "get_cell_state", "input": {"cell_id": "C"}}}], "tool_use"),
        _reply([{"text": '{"ok": true}'}]),
    ]
    assert _run_coverage(agent_cp) == {"ok": True}

    all_spans = spans.get_finished_spans()
    kinds = {s.name: s.attributes.get("openinference.span.kind") for s in all_spans}
    agent = next(s for s in all_spans if s.name == "invoke_agent coverage")
    llms = [s for s in all_spans if s.name == "chat"]
    tool = next(s for s in all_spans if s.name == "execute_tool get_cell_state")
    assert kinds["invoke_agent coverage"] == "AGENT" and kinds["chat"] == "LLM" and kinds["execute_tool get_cell_state"] == "TOOL"
    assert len(llms) == 2 and agent.attributes["output.value"].strip() == '{"ok": true}'
    assert '"cell_id": "C"' in tool.attributes["input.value"] and '"enabled": 0' in tool.attributes["output.value"]
    assert any(k.startswith("gen_ai.") for k in llms[0].attributes)  # Dynatrace and CloudWatch read these
    assert agent.attributes["track3.run_id"] == "test-run"
    assert all(s.context.trace_id == int(TRACE, 16) for s in all_spans)  # one run, one trace id
    assert agent.status.status_code == StatusCode.OK


def test_the_checkpoint_still_decides_and_records_every_call(spans, agent_cp, bedrock):
    bedrock.converse.side_effect = [
        _reply([{"toolUse": {"toolUseId": "t1", "name": "get_cell_state", "input": {"cell_id": "C"}}}], "tool_use"),
        _reply([{"text": '{"ok": true}'}]),
    ]
    _run_coverage(agent_cp)
    recs = _records(agent_cp)
    kinds = [r["kind"] for r in recs]
    decisions = [r["action"] for r in recs if r["kind"] == "decision"]
    assert decisions == ["invoke_model", "get_cell_state", "invoke_model"], decisions
    assert kinds.count("model_call") == 2 and kinds.count("tool_call") == 1
    call = next(r for r in recs if r["kind"] == "model_call")
    assert call["request_id"] == "req-1" and call["input_tokens"] == 11 and call["tool_calls"] == ["get_cell_state"]
    tool = next(r for r in recs if r["kind"] == "tool_call")
    assert tool["args"] == {"cell_id": "C"} and '"enabled": 0' in tool["result"]
    assert [r["source"] for r in recs if r["kind"] == "guardrail"] == ["INPUT", "INPUT", "OUTPUT"]  # prompt, tool result, answer


def test_a_denied_tool_is_cancelled_and_the_model_told(spans, agent_cp, bedrock, monkeypatch):
    monkeypatch.setattr(agent_cp, "evaluate", lambda p, action, *a, **k:
                        ("Deny", ["power-range"], []) if action == "get_cell_state" else ("Allow", ["test-allow"], []))
    ran = []
    bedrock.converse.side_effect = [
        _reply([{"toolUse": {"toolUseId": "t1", "name": "get_cell_state", "input": {"cell_id": "C"}}}], "tool_use"),
        _reply([{"text": '{"ok": true}'}]),
    ]
    _run_coverage(agent_cp, {"get_cell_state": lambda cell_id: ran.append(cell_id)})
    assert ran == []  # Cedar said no, so the tool never ran
    second_call = bedrock.converse.call_args_list[1].kwargs["messages"]
    assert "refused by policy: power-range" in json.dumps(second_call)
    assert "refused by policy: power-range" in next(r for r in _records(agent_cp) if r["kind"] == "tool_call")["result"]


def test_a_withdrawn_model_is_refused_before_bedrock_is_called(spans, agent_cp, bedrock, monkeypatch):
    from checkpoint import Refused

    monkeypatch.setattr(agent_cp, "model_status", lambda model_id: "withdrawn")
    monkeypatch.setattr(agent_cp, "evaluate", lambda p, action, *a, **k:
                        ("Deny", ["no policy permits this call"], []) if action == "invoke_model" else ("Allow", [], []))
    with pytest.raises(Refused):
        _run_coverage(agent_cp)
    bedrock.converse.assert_not_called()
    assert any(r["kind"] == "refused" and r["who"] == "coverage" for r in _records(agent_cp))


def test_a_misnamed_tool_is_repaired_before_strands_sees_it(spans, agent_cp, bedrock):
    bedrock.converse.side_effect = [
        _reply([{"toolUse": {"toolUseId": "t1", "name": "functions.get_cell_state", "input": {"cell_id": "C"}}}], "tool_use"),
        _reply([{"text": '{"ok": true}'}]),
    ]
    assert _run_coverage(agent_cp) == {"ok": True}
    repaired = next(r for r in _records(agent_cp) if r["kind"] == "tool_name_repaired")
    assert repaired["requested"] == ["functions.get_cell_state"] and repaired["used"] == ["get_cell_state"]


def test_a_bad_answer_gets_one_retry_then_fails(spans, agent_cp, bedrock):
    from agents.governance import AgentFailed

    bedrock.converse.side_effect = [_reply([{"text": "not json"}]), _reply([{"text": "still not json"}])]
    with pytest.raises(AgentFailed):
        _run_coverage(agent_cp, {})
    kinds = [r["kind"] for r in _records(agent_cp)]
    assert kinds.count("contract_retry") == 1 and kinds[-1] == "agent_failed"


def test_the_judge_runs_on_strands_too(spans, agent_cp, bedrock):
    bedrock.converse.side_effect = [_reply([{"text": '{"score": 4}'}])]
    out = agent_cp.call_model("judge", "us.anthropic.claude-haiku-5-5", "grade this", "the run")
    assert out["text"] == '{"score": 4}' and out["usage"]["inputTokens"] == 11
    assert "temperature" not in bedrock.converse.call_args.kwargs.get("inferenceConfig", {})  # deprecated for this model
    assert next(s for s in spans.get_finished_spans() if s.name == "invoke_agent judge").attributes["openinference.span.kind"] == "AGENT"


def test_arize_gets_only_openinference_spans(spans, fake_mirrors, monkeypatch, tmp_path, agent_cp, bedrock):
    monkeypatch.setenv("ARIZE_API_KEY", "test-key")
    checkpoint.mirror_run(TRACE)
    bedrock.converse.side_effect = [_reply([{"text": '{"ok": true}'}])]
    _run_coverage(agent_cp, {})
    tracer = agent_cp.tracer  # a raw span with no OpenInference kind, like ADOT's boto3 spans
    with agent_cp.context():
        tracer.start_span("Bedrock Runtime.Converse").end()
    import telemetry
    telemetry.flush()
    arize = next(e for e in fake_mirrors if "arize" in e.endpoint)
    names = {s.name for s in arize.spans}
    assert "invoke_agent coverage" in names and "chat" in names and "Bedrock Runtime.Converse" not in names


def test_another_runtime_hangs_under_the_orchestrators_invoke_span(spans, agent_cp, monkeypatch):
    from agents import cloud

    monkeypatch.setattr(cloud, "_runtime_arn", lambda role: f"arn:aws:bedrock-agentcore:us-east-2:1:runtime/{role}")
    remote = cloud.RemoteAgents({"run_id": "test-run", "trace_id": TRACE, "parent_span_id": "0" * 16})
    sent = {}

    def fake_invoke(**kw):
        sent.update(json.loads(kw["payload"]), traceparent=kw.get("traceParent"))
        return {"response": MagicMock(read=lambda: b'{"report": {"domain_verdict": "ok"}}')}

    remote.client = MagicMock(invoke_agent_runtime=fake_invoke)
    run = MagicMock(cp=agent_cp, alert="tower C is off")
    run.net.snapshot = {"snapshot_sha256": "sha256:x"}
    assert remote.specialist(run, "coverage") == {"domain_verdict": "ok"}
    invoke = next(s for s in spans.get_finished_spans() if s.name == "invoke coverage")
    assert sent["parent_span_id"] == format(invoke.context.span_id, "016x")
    assert sent["traceparent"] == f"00-{TRACE}-{invoke.context.span_id:016x}-01"  # the W3C header, same parent


def _child_checkpoint(tmp_path, parent_span_id=""):
    return checkpoint.Checkpoint("child-run", TRACE, tmp_path / "child", parent_span_id)


def test_a_runtime_continues_the_trace_from_the_traceparent_header(spans, offline_cp, tmp_path):
    from opentelemetry import context as otel_context

    # What the runtime's instrumentation does with an incoming traceparent: it becomes the current context.
    incoming = trace.SpanContext(int(TRACE, 16), int(PARENT, 16), is_remote=True, trace_flags=trace.TraceFlags(1))
    token = otel_context.attach(trace.set_span_in_context(trace.NonRecordingSpan(incoming)))
    try:
        cp = _child_checkpoint(tmp_path, parent_span_id="1" * 16)  # the payload says otherwise: the header wins
    finally:
        otel_context.detach(token)
    cp.close()
    (root,) = finished(spans, "track3 child-run")
    assert root.parent.span_id == int(PARENT, 16) and root.attributes["track3.parent_from"] == "traceparent"


def test_without_the_header_a_runtime_uses_the_payload_parent(spans, offline_cp, tmp_path):
    cp = _child_checkpoint(tmp_path, parent_span_id=PARENT)
    cp.close()
    (root,) = finished(spans, "track3 child-run")
    assert root.parent.span_id == int(PARENT, 16) and root.attributes["track3.parent_from"] == "payload"


def test_a_header_from_another_trace_is_ignored(spans, offline_cp, tmp_path):
    from opentelemetry import context as otel_context

    other = trace.SpanContext(int("ab" * 16, 16), int(PARENT, 16), is_remote=True, trace_flags=trace.TraceFlags(1))
    token = otel_context.attach(trace.set_span_in_context(trace.NonRecordingSpan(other)))
    try:
        cp = _child_checkpoint(tmp_path)
    finally:
        otel_context.detach(token)
    cp.close()
    (root,) = finished(spans, "track3 child-run")
    assert root.parent is None and root.context.trace_id == int(TRACE, 16) and root.attributes["track3.parent_from"] == "none"
