"""Cell network ops agent: watches for the safety problem, asks Claude what happened, writes advice back to Dynatrace.

Loop: Dynatrace problem → read network state (Grail) → Claude → advice log in Dynatrace → optional rollback to v1.
Every run is traced with OpenTelemetry (GenAI + OpenInference attributes) and exported to Arize and, once
DT_TRACES_TOKEN is set (scope openpipeline:traces:ingest), to Dynatrace AI Observability too.

  ./agent.sh            watch and advise
  ./agent.sh --act      also roll the simulator back to v1 when Claude recommends it
  ./agent.sh --once     handle the current open problem once, then exit
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

HERE = Path(__file__).parent
CONTROL = HERE / "control.json"
TENANT = "https://yqs73977.sprint.dynatracelabs.com"
LOG_INGEST = "https://yqs73977.sprint.apps.dynatracelabs.com/platform/classic/environment-api/v2/logs/ingest"
ARIZE_SPACE_ID = "U3BhY2U6NTI3NjU6L2tJWQ=="
ARIZE_KEY = Path.home() / ".arize_api_key"
PROJECT = "cell-network-ops-agent"
STATE_DIR = Path.home() / ".local/state/cell-ops-agent"  # empty folder, so Claude runs with no project context

SYSTEM_PROMPT = """You are the operations agent for a mobile network. A Dynatrace problem says a radio policy model broke the
safety rule: phones within range of a tower were left without signal. You get the problem, the live network state,
and a comparison of model v1 (the current safe model) with model v2 (the candidate).
Explain in plain words what the model did, name the towers and phones involved, and recommend one action.
Reply with JSON only, no markdown:
{"summary": "<= 25 words, what happened",
 "root_cause": "<= 30 words, why, citing the numbers",
 "evidence": ["<= 12 words", "<= 12 words", "<= 12 words"],
 "action": "rollback_to_v1" or "keep_watching",
 "confidence": 0.0 to 1.0}"""


def setup_tracing():
    provider = TracerProvider(resource=Resource.create({
        "service.name": PROJECT,
        "openinference.project.name": PROJECT,
        "deployment.environment": "breaking-barriers-demo",
    }))
    targets = []
    if ARIZE_KEY.exists():
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint="https://otlp.arize.com/v1/traces",
            headers={"space_id": ARIZE_SPACE_ID, "api_key": ARIZE_KEY.read_text().strip()})))
        targets.append("Arize")
    token = os.environ.get("DT_TRACES_TOKEN", "")
    if token:
        scheme = "Api-Token" if token.startswith("dt0c01") else "Bearer"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=f"{TENANT}/api/v2/otlp/v1/traces", headers={"Authorization": f"{scheme} {token}"})))
        targets.append("Dynatrace")
    trace.set_tracer_provider(provider)
    return provider, targets


def dql(query):
    # --agent forces the JSON envelope; without it dtctl prints a different shape outside AI tools.
    out = subprocess.run(["dtctl", "--context", "astroshop", "query", query, "-o", "json", "--agent"],
                         capture_output=True, text=True, timeout=60)
    reply = json.loads(out.stdout or "{}")
    if not reply.get("ok"):
        raise RuntimeError(f"dtctl query failed: {(out.stderr or out.stdout)[:300]}")
    body = reply.get("result") or {}
    if body.get("kind") == "result-file":
        return [json.loads(line) for line in open(body["path"])]
    return body.get("records") or []


def open_problem():
    # Filter on ACTIVE: closed problems get late updates and would otherwise hide a newer open one.
    rows = dql('fetch dt.davis.problems, from: now()-2h | filter event.name == "Cell network safety rule broken" '
               '| filter event.status == "ACTIVE" | sort event.start desc | limit 1 '
               '| fields display_id, event.id, event.status, event.start, event.description')
    return rows[0] if rows else None


def network_state():
    step = dql('fetch logs, from: now()-10m | filter service.name == "mobile-env" and mobile.kind == "step" '
               '| sort timestamp desc | limit 1 | fields timestamp, mobile.policy, mobile.phones_active, '
               'mobile.phones_served, mobile.phones_dropped, mobile.median_rate, mobile.ues, mobile.towers')
    compare = dql('fetch logs, from: now()-15m | filter service.name == "mobile-env" and mobile.kind == "step" '
                  '| summarize steps = count(), dropped_per_step = avg(toDouble(mobile.phones_dropped)), '
                  'median_rate = avg(toDouble(mobile.median_rate)), by: {model = mobile.policy}')
    if not step:
        return {}
    s = step[0]
    towers = json.loads(s.get("mobile.towers") or "[]")
    phones = json.loads(s.get("mobile.ues") or "[]")
    dropped = []
    for p in phones:
        if p["state"] != "dropped":
            continue
        near = sorted((round(((p["x"] - t["x"]) ** 2 + (p["y"] - t["y"]) ** 2) ** 0.5, 1), t["name"], t["load"])
                      for t in towers)
        in_range = [{"tower": n, "distance": d, "tower_load": load} for d, n, load in near if d <= towers[0]["range"]]
        dropped.append({"phone": f"phone-{p['id']:02d}", "towers_in_range": in_range})
    return {
        "model_running": s.get("mobile.policy"),
        "phones_active": s.get("mobile.phones_active"),
        "phones_served": s.get("mobile.phones_served"),
        "phones_dropped": s.get("mobile.phones_dropped"),
        "median_rate_served": s.get("mobile.median_rate"),
        "tower_load": {t["name"]: t["load"] for t in towers},
        "dropped_phones": dropped,
        "v1_vs_v2_last_15_min": [
            {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in compare],
    }


def ask_claude(context):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    out = subprocess.run(
        ["claude", "-p", json.dumps(context), "--system-prompt", SYSTEM_PROMPT, "--tools", "",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--output-format", "json",
         "--max-turns", "1", "--settings", '{"disableAllHooks": true}'],
        capture_output=True, text=True, timeout=120, cwd=STATE_DIR)
    meta = json.loads(out.stdout)
    model, usage = next(iter(meta.get("modelUsage", {}).items()), ("claude", {}))
    text = meta.get("result", "").strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(text), {
        "model": model,
        "input_tokens": usage.get("inputTokens", 0) + usage.get("cacheReadInputTokens", 0)
        + usage.get("cacheCreationInputTokens", 0),
        "output_tokens": usage.get("outputTokens", 0),
        "cost_usd": round(meta.get("total_cost_usd", 0.0), 5),
        "duration_ms": meta.get("duration_ms", 0),
        "raw": meta.get("result", ""),
    }


def write_log(record):
    req = urllib.request.Request(LOG_INGEST, data=json.dumps([record]).encode(), headers={
        "Authorization": f"Bearer {os.environ['DT_PLATFORM_TOKEN']}", "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def tool_span(tracer, name):
    span = tracer.start_as_current_span(f"execute_tool {name}")
    return span


def status(state, text, problem=""):
    """Heartbeat for the app's AI panel: watching / thinking."""
    write_log({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "service.name": "cell-ops-agent", "loglevel": "INFO", "log.source": "breaking-barriers/agent.py",
        "content": text, "agent.state": state, "agent.problem": problem,
    })


def handle(tracer, problem, act):
    pid = problem["display_id"]
    status("thinking", f"{pid} is open. Reading the network and asking Claude.", pid)
    with tracer.start_as_current_span("invoke_agent cell-ops-agent") as root:
        root.set_attributes({
            "openinference.span.kind": "AGENT", "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "cell-ops-agent", "cell.problem": pid,
            "input.value": f"Investigate {pid}: {problem.get('event.description', '')[:300]}",
        })
        trace_id = format(root.get_span_context().trace_id, "032x")

        with tool_span(tracer, "read_network_state") as sp:
            sp.set_attributes({"openinference.span.kind": "TOOL", "gen_ai.operation.name": "execute_tool",
                               "gen_ai.tool.name": "read_network_state", "tool.name": "read_network_state"})
            context = {"problem": {"id": pid, "started": problem.get("event.start")}, **network_state()}
            sp.set_attribute("output.value", json.dumps(context)[:4000])

        with tracer.start_as_current_span("chat claude") as sp:
            sp.set_attributes({"openinference.span.kind": "LLM", "gen_ai.operation.name": "chat",
                               "gen_ai.system": "anthropic", "gen_ai.provider.name": "anthropic",
                               "llm.provider": "anthropic", "llm.system": "anthropic",
                               "input.value": json.dumps(context)[:8000],
                               "llm.input_messages.0.message.role": "system",
                               "llm.input_messages.0.message.content": SYSTEM_PROMPT,
                               "llm.input_messages.1.message.role": "user",
                               "llm.input_messages.1.message.content": json.dumps(context)[:8000]})
            advice, usage = ask_claude(context)
            sp.set_attributes({
                "gen_ai.request.model": usage["model"], "gen_ai.response.model": usage["model"],
                "llm.model_name": usage["model"],
                "gen_ai.usage.input_tokens": usage["input_tokens"], "gen_ai.usage.output_tokens": usage["output_tokens"],
                "llm.token_count.prompt": usage["input_tokens"], "llm.token_count.completion": usage["output_tokens"],
                "llm.token_count.total": usage["input_tokens"] + usage["output_tokens"],
                "gen_ai.usage.cost": usage["cost_usd"],
                "output.value": usage["raw"],
                "llm.output_messages.0.message.role": "assistant",
                "llm.output_messages.0.message.content": usage["raw"],
            })

        acted = False
        if act and advice.get("action") == "rollback_to_v1":
            with tool_span(tracer, "rollback_to_v1") as sp:
                sp.set_attributes({"openinference.span.kind": "TOOL", "gen_ai.operation.name": "execute_tool",
                                   "gen_ai.tool.name": "rollback_to_v1", "tool.name": "rollback_to_v1"})
                CONTROL.write_text(json.dumps({"policy": "v1", "by": "cell-ops-agent", "problem": pid,
                                               "at": datetime.now(timezone.utc).isoformat()}))
                acted = True
                sp.set_attribute("output.value", "Simulator switched back to model v1")

        with tool_span(tracer, "write_advice_to_dynatrace") as sp:
            sp.set_attributes({"openinference.span.kind": "TOOL", "gen_ai.operation.name": "execute_tool",
                               "gen_ai.tool.name": "write_advice_to_dynatrace", "tool.name": "write_advice_to_dynatrace"})
            write_log({
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "service.name": "cell-ops-agent", "loglevel": "INFO", "log.source": "breaking-barriers/agent.py",
                "content": advice.get("summary", ""),
                "agent.state": "acted" if acted else "advised",
                "agent.problem": pid, "agent.problem_event_id": problem.get("event.id"),
                "agent.root_cause": advice.get("root_cause", ""),
                "agent.evidence": json.dumps(advice.get("evidence", [])),
                "agent.action": advice.get("action", ""), "agent.acted": acted,
                "agent.confidence": advice.get("confidence"),
                "agent.model": usage["model"], "agent.input_tokens": usage["input_tokens"],
                "agent.output_tokens": usage["output_tokens"], "agent.cost_usd": usage["cost_usd"],
                "agent.duration_ms": usage["duration_ms"], "agent.trace_id": trace_id,
            })

        root.set_attribute("output.value", json.dumps(advice))
        root.set_status(Status(StatusCode.OK))
    return advice, usage, acted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--act", action="store_true", help="roll the simulator back to v1 when Claude recommends it")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=int, default=15, help="seconds between checks")
    args = ap.parse_args()

    provider, targets = setup_tracing()
    tracer = trace.get_tracer("cell-ops-agent")
    if not args.once:
        print(f"cell-ops-agent · traces → {', '.join(targets) or 'nowhere'} · act={args.act}", flush=True)
    if "Dynatrace" not in targets and not args.once:
        print("  (add DT_TRACES_TOKEN with scope openpipeline:traces:ingest to ~/.bindplane/.env to trace into Dynatrace)",
              flush=True)

    # Remember across runs, so ./run-demo.sh can start a fresh --once check every 15 s.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handled_file, beat_file = STATE_DIR / "handled.txt", STATE_DIR / "last_beat"
    handled = set(handled_file.read_text().split()) if handled_file.exists() else set()
    last_beat = float(beat_file.read_text()) if beat_file.exists() else 0.0
    while True:
        try:
            problem = open_problem()
            if problem and problem["display_id"] not in handled:
                print(f"{datetime.now():%H:%M:%S} {problem['display_id']} open → asking Claude", flush=True)
                advice, usage, acted = handle(tracer, problem, args.act)
                handled.add(problem["display_id"])
                handled_file.write_text("\n".join(sorted(handled)))
                print(f"  {advice.get('summary')}\n  action: {advice.get('action')}{' (done)' if acted else ''} · "
                      f"{usage['model']} · {usage['input_tokens']}+{usage['output_tokens']} tokens · ${usage['cost_usd']}",
                      flush=True)
                provider.force_flush()
                last_beat = time.time()
            elif not problem and time.time() - last_beat > 30:
                status("watching", "Watching Dynatrace for cell network problems.")
                last_beat = time.time()
                beat_file.write_text(str(last_beat))
        except Exception as e:  # keep watching through network blips
            print(f"{datetime.now():%H:%M:%S} error: {e}", file=sys.stderr, flush=True)
        if args.once:
            break
        time.sleep(args.every)
    provider.shutdown()


if __name__ == "__main__":
    main()
