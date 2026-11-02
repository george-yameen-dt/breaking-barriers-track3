"""The Risk & Policy critic: the first agent to see all three specialists' reports side by side.

It must run the deterministic run_risk_checks tool on every proposal. The checks and the cross-agent view come from that
tool, never from the model; the model writes the risk, the recommendation and the reasons. Its advice is not a control:
Cedar decides what may be applied.
"""

import json
from pathlib import Path

from agents.governance import run_agent
from contracts import CRITIC, ORCHESTRATOR, assessments_problems

MODEL = "openai.gpt-oss-120b-1:0"
PROMPT = Path(__file__).parent / "prompts" / "risk.md"


def run_critic(run) -> dict:
    """In one process: the orchestrator's hand-off (a Cedar decision), then the critic's own work."""
    run.cp.handoff(ORCHESTRATOR, CRITIC, f"{len(run.proposals)} proposals from {len(run.reports)} specialists")
    return assess_proposals(run)


def assess_proposals(run) -> dict:
    """The critic's assessments by proposal id (risk, recommendation, reasons), wherever it runs."""
    cp = run.cp
    ids = [p["proposal_id"] for p in run.proposals]
    payload = {"alert": run.alert, "snapshot": run.net.snapshot, "reports": run.reports,
               "proposals": [{k: v for k, v in p.items() if k != "rollback"} for p in run.proposals]}
    answer = run_agent(cp, CRITIC, MODEL, PROMPT.read_text(), json.dumps(payload), run.tools_for(CRITIC),
                       validate=lambda a: assessments_problems(a, ids), context_for=run.context_for, max_turns=10)
    return {a["proposal_id"]: a for a in answer.get("assessments", []) if a.get("proposal_id") in ids}
