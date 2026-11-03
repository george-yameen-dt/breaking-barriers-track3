"""The three domain specialists. Each gets only the KPI snapshot, investigates on its own with its tools and returns a
SpecialistReport (contracts.py). None of them sees another's report before it answers.

  coverage      radio power and tilt: coverage holes, overshoot, interference   gpt-oss-120b
  capacity      crowded cells: moving load to quieter neighbours                gpt-oss-20b (the pause demo withdraws it)
  availability  dead cells: switch back on, or neighbours cover the gap         gpt-oss-120b
"""

import json
from pathlib import Path

from agents.governance import run_agent
from contracts import ORCHESTRATOR, report_problems

REASONING_BUDGET = 10000  # gpt-oss reasons before it answers; 3000 tokens ended the capacity specialist mid-thought
MODELS = {"coverage": "openai.gpt-oss-120b-1:0", "capacity": "openai.gpt-oss-20b-1:0", "availability": "openai.gpt-oss-120b-1:0"}
PROMPTS = Path(__file__).parent / "prompts"


def _dynatrace_note(tools: dict) -> str:
    if "dt_query" not in tools:
        return "\n\nDynatrace tools are not available in this run: work from the snapshot and your network tools."
    return ("\n\nDynatrace tools are available (dtctl, read-only): dt_problems, dt_log_patterns, dt_query, and load_skill for "
            "the Dynatrace for AI skills (dt-dql-essentials, dt-obs-logs, dt-obs-problems, dt-obs-tracing). For every DQL "
            "result you rely on, add {\"dql\": ..., \"result_sha256\": ...} to that finding's dql_refs.")


def run_specialist(run, role: str) -> dict:
    """In one process: the orchestrator's hand-off (a Cedar decision), then the specialist's own work."""
    run.cp.handoff(ORCHESTRATOR, role, run.alert)
    return investigate(run, role)


def investigate(run, role: str) -> dict:
    """The specialist's own work, wherever it runs (this process or its own AgentCore runtime)."""
    cp, snap = run.cp, run.net.snapshot
    tools = run.tools_for(role)
    system = (PROMPTS / f"{role}.md").read_text() + getattr(run.net, "prompt_note", "") + _dynatrace_note(tools)
    payload = {"run_id": cp.run_id, "trace_id": cp.trace_id, "alert": run.alert, "snapshot": snap}
    report = run_agent(cp, role, MODELS[role], system, json.dumps(payload), tools, validate=report_problems,
                       context_for=run.context_for, max_turns=10, max_tokens=REASONING_BUDGET)
    report.update(agent=role, snapshot_sha256=snap["snapshot_sha256"])
    cp.record("specialist_report", who=role, model_id=MODELS[role], report=report)
    return report
