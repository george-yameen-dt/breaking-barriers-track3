"""Every agent runs on Strands; the checkpoint sits in front of each of its model and tool calls.

Strands runs the loop (model -> tools -> model ...) and traces it: an AGENT span per turn, an LLM span per model call,
a TOOL span per tool call (telemetry.py converts them for Arize). The governance stays ours, in two places:
- the Bedrock client's `converse` is wrapped: model registry + Cedar `invoke_model` before the call (a withdrawn model
  raises Refused), then tool-name repair, Bedrock Guardrails on the output, and the `model_call` record with its
  Bedrock request id, so Strands only ever sees a screened, well-formed answer;
- Strands' tool hooks: Cedar decides before each tool call (a Deny cancels it), and after it the result is recorded and
  screened by Bedrock Guardrails before the model sees it.
The record kinds and fields are the ones tools/check.py grades; only the loop under them changed.
"""

import contextlib
import json
import re
import time

import boto3

import telemetry
from checkpoint import REGION, Refused, sha256

from strands import Agent  # noqa: E402  (after telemetry, so Strands finds the run's tracer provider)
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry  # noqa: E402
from strands.models.bedrock import BedrockModel  # noqa: E402
from strands.tools.executors import SequentialToolExecutor  # noqa: E402
from strands.tools.tools import PythonAgentTool  # noqa: E402

NETWORK = 'Network::"mobile-env-medium"'
MAX_TOOL_RESULT = 6000


class AgentFailed(Exception):
    """An agent ran out of turns or answered outside its contract."""


def _valid_tool_names(message: dict, tools: dict) -> list:
    """gpt-oss sometimes names a tool in a form Bedrock refuses to take back (e.g. 'functions.get_cell_state').
    Rewrites the message in place to the known tool the name contains, or 'unknown_tool' (which the hook refuses).
    Returns the (requested, used) pairs that changed."""
    changed = []
    for block in message.get("content", []):
        use = block.get("toolUse")
        if use and not re.fullmatch(r"[a-zA-Z0-9_-]+", use.get("name") or ""):
            known = [p for p in re.findall(r"[a-zA-Z0-9_-]+", use.get("name") or "") if p in tools]
            changed.append((use.get("name", ""), known[-1] if known else "unknown_tool"))
            use["name"] = changed[-1][1]
    return changed


def _replace_text(message: dict, text: str) -> None:
    """The guardrail's version of the text, in place of the message's text blocks (other blocks kept in order)."""
    blocks, done = [], False
    for block in message["content"]:
        if "text" in block:
            if not done:
                blocks.append({"text": text})
                done = True
        else:
            blocks.append(block)
    message["content"] = blocks


class _GovernedModel:
    """Wraps one agent's Bedrock client: every Converse call crosses the checkpoint first and is recorded after."""

    def __init__(self, cp, who: str, model_id: str, system: str, prompt: str, tools: dict, max_turns: int):
        self.cp, self.who, self.model_id, self.tools, self.max_turns = cp, who, model_id, tools, max_turns
        self.prompt_sha256 = sha256(system + "\n" + prompt)
        self.turn, self.usage = 0, {}

    def wrap(self, client) -> None:
        original = client.converse
        client.converse = lambda **request: self.converse(original, **request)

    def converse(self, original, **request) -> dict:
        cp, who = self.cp, self.who
        self.turn += 1
        if self.turn > self.max_turns:
            cp.record("agent_failed", who=who, model_id=self.model_id, reason=f"no answer within {self.max_turns} turns")
            raise AgentFailed(f"{who}: no answer within {self.max_turns} turns")
        status = cp.model_status(self.model_id)  # read every turn: a pause takes effect mid-run
        d = cp.decide(f'Agent::"{who}"', "invoke_model", f'Model::"{self.model_id}"', {}, {"status": status})
        if d["decision"] != "Allow":
            cp.record("refused", who=who, model_id=self.model_id, reason=f"model is {status}")
            raise Refused(f"{self.model_id} is {status}: calls are refused")
        if self.tools and self.turn == self.max_turns and request.get("messages"):
            last = request["messages"][-1]
            request["messages"] = [*request["messages"][:-1], {**last, "content": [
                *last["content"], {"text": "This is your last turn: reply now with the JSON answer only."}]}]
        t0 = time.time()
        try:
            r = original(**request)
        except Exception as e:  # a failed call is a result too: record it before it propagates
            meta = getattr(e, "response", {}).get("ResponseMetadata", {}) if hasattr(e, "response") else {}
            cp.record("model_call_failed", who=who, model_id=self.model_id, turn=self.turn, request_id=meta.get("RequestId", ""),
                      latency_ms=int((time.time() - t0) * 1000), error=str(e)[:500], prompt_sha256=self.prompt_sha256)
            raise
        message = r["output"]["message"]
        repaired = _valid_tool_names(message, self.tools)
        if repaired:
            cp.record("tool_name_repaired", who=who, requested=[a for a, _ in repaired], used=[b for _, b in repaired])
        text = "".join(block.get("text", "") for block in message["content"])
        if text.strip():
            screened = cp.guard("OUTPUT", text, who)
            if screened != text:
                _replace_text(message, screened)
                text = screened
        uses = [block["toolUse"] for block in message["content"] if "toolUse" in block]
        self.usage = r.get("usage", {})
        cp.record("model_call", who=who, model_id=self.model_id, turn=self.turn, request_id=r["ResponseMetadata"]["RequestId"],
                  latency_ms=int((time.time() - t0) * 1000), input_tokens=self.usage.get("inputTokens", 0),
                  output_tokens=self.usage.get("outputTokens", 0), stop_reason=r.get("stopReason", ""),
                  tool_calls=[u.get("name", "") for u in uses], prompt_sha256=self.prompt_sha256,
                  output_sha256=sha256(text), output=text[:2000])
        return r


class _ToolCheckpoint(HookProvider):
    """Cedar before every tool call; the record and Bedrock Guardrails on its result after."""

    def __init__(self, cp, who: str, tools: dict, context_for):
        self.cp, self.who, self.tools, self.context_for = cp, who, tools, context_for

    def register_hooks(self, registry: HookRegistry, **_) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)

    def before(self, event: BeforeToolCallEvent) -> None:
        name, args = event.tool_use.get("name", ""), event.tool_use.get("input") or {}
        if name not in self.tools:
            self.cp.record("tool_refused", who=self.who, tool=name, args=args, reason="not one of this agent's tools")
            event.cancel_tool = json.dumps({"error": f"{name!r} is not one of your tools"})
            return
        context = self.context_for(name, args) if self.context_for else {}
        d = self.cp.decide(f'Agent::"{self.who}"', name, NETWORK, context)
        if d["decision"] != "Allow":
            event.cancel_tool = json.dumps({"error": "refused by policy: " + ", ".join(d["policies"])})

    def after(self, event: AfterToolCallEvent) -> None:
        name, args = event.tool_use.get("name", ""), event.tool_use.get("input") or {}
        out = "".join(c.get("text", "") or json.dumps(c.get("json"), sort_keys=True) for c in event.result.get("content", []))
        self.cp.record("tool_call", who=self.who, tool=name, tool_call_id=event.tool_use.get("toolUseId", ""), args=args,
                       result_sha256=sha256(out), result=out[:900])
        try:
            self.cp.guard("INPUT", out, self.who)
        except Refused:
            event.result = {**event.result, "status": "error",
                            "content": [{"text": json.dumps({"error": "the guardrail blocked this tool result"})}]}


def _tool(name: str, spec: dict, fn) -> PythonAgentTool:
    """One of our tool functions as a Strands tool, with its contract (contracts.TOOLS) as the spec."""
    def call(tool_use, **_):
        try:
            result = fn(**(tool_use.get("input") or {}))
        except TypeError as e:
            result = {"error": f"bad arguments: {e}"}
        except Exception as e:  # a broken tool is an answer the agent can work with, not a crash
            result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
        out = json.dumps(result, sort_keys=True, default=str)[:MAX_TOOL_RESULT]
        return {"toolUseId": tool_use["toolUseId"], "status": "success", "content": [{"text": out}]}
    return PythonAgentTool(name, spec["toolSpec"], call)


def make_agent(cp, who: str, model_id: str, system: str, prompt: str, tools: dict, specs: dict, context_for=None,
               max_turns: int = 8, max_tokens: int = 3000) -> tuple:
    """A Strands agent whose every model and tool call crosses the checkpoint. Returns (agent, governed model)."""
    telemetry.provider()  # before Strands makes its tracer, so it uses the run's provider
    model = BedrockModel(model_id=model_id, boto_session=boto3.Session(region_name=REGION), streaming=False,
                         max_tokens=max_tokens, **({} if "anthropic" in model_id else {"temperature": 0.2}))
    governed = _GovernedModel(cp, who, model_id, system, prompt, tools, max_turns)
    governed.wrap(model.client)
    agent = Agent(model=model, system_prompt=system, name=who, callback_handler=None,
                  tools=[_tool(name, specs[name], fn) for name, fn in tools.items()],
                  hooks=[_ToolCheckpoint(cp, who, tools, context_for)], tool_executor=SequentialToolExecutor(),
                  trace_attributes={"track3.run_id": cp.run_id, "track3.who": who, "session.id": cp.run_id})
    return agent, governed


@contextlib.contextmanager
def in_run(cp):
    """Strands parents its spans to the current span: make that the run's, so they join the run's trace."""
    with cp.context():
        yield


def ask(agent, cp, text: str) -> str:
    with in_run(cp):
        return str(agent(text)).strip()
