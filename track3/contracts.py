"""Contracts for the Track 3 swarm: the shapes the five agents and their tools exchange, and the hashes that bind them.

The five agents: three domain specialists (coverage, capacity, availability), the risk critic and the orchestrator.
Values Cedar compares are integers. The Converse tool definitions live here, so the prompts, the tool loop and the
checkpoint share one list of tools.
"""

import hashlib
import json

SPECIALISTS = ("coverage", "capacity", "availability")
CRITIC, ORCHESTRATOR = "risk", "orchestrator"
AGENTS = (*SPECIALISTS, CRITIC, ORCHESTRATOR)
PARAMETERS = ("enabled", "tx_power_dbm", "tilt_deg")
ACTIONS = {"enabled": "set_cell_state", "tx_power_dbm": "adjust_tx_power", "tilt_deg": "adjust_tilt"}
VERDICTS = ("issue_found", "no_issue_in_domain", "out_of_domain_issue_seen")
RISKS = ("low", "medium", "high")
RECOMMENDATIONS = ("approval_required", "reject", "needs_more_evidence")
SKILLS = ("dt-dql-essentials", "dt-obs-logs", "dt-obs-problems", "dt-obs-tracing")


def canonical_json(obj) -> str:
    """Sorted keys, no spaces: the one form every hash is taken over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_of(obj) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def _tool(name: str, description: str, properties: dict, required: list) -> dict:
    return {"toolSpec": {"name": name, "description": description,
                         "inputSchema": {"json": {"type": "object", "properties": properties, "required": required}}}}


_CELL = {"cell_id": {"type": "string", "description": "the cell, A to G"}}
TOOLS = {
    "get_cell_state": _tool("get_cell_state", "One cell's KPIs and settings from the snapshot.", _CELL, ["cell_id"]),
    "get_neighbor_state": _tool("get_neighbor_state", "The KPIs of a cell's three nearest neighbours.", _CELL, ["cell_id"]),
    "get_current_configuration": _tool(
        "get_current_configuration",
        "A cell's settings: what it should run (desired), what the snapshot shows (observed), and whether they match.",
        _CELL, ["cell_id"]),
    "get_recent_alarm_history": _tool(
        "get_recent_alarm_history", "Alarms and configuration changes on one cell, newest first.",
        {**_CELL, "window_minutes": {"type": "integer", "description": "how far back, default 60"}}, ["cell_id"]),
    "simulate_power_change": _tool(
        "simulate_power_change",
        "Predict the network if one cell's transmit power changes by delta_db. Returns predicted KPIs, deltas and the "
        "simulation_ref to copy into a proposal.",
        {**_CELL, "delta_db": {"type": "integer", "description": "dB to add, for example -3 or 2"}}, ["cell_id", "delta_db"]),
    "simulate_tilt_change": _tool(
        "simulate_tilt_change",
        "Predict the network if one cell's downtilt changes by delta_deg (positive points the beam lower: a smaller area). "
        "Returns predicted KPIs, deltas and the simulation_ref.",
        {**_CELL, "delta_deg": {"type": "integer"}}, ["cell_id", "delta_deg"]),
    "simulate_cell_state": _tool(
        "simulate_cell_state", "Predict the network if one cell is switched on (1) or off (0). Returns the simulation_ref.",
        {**_CELL, "enabled": {"type": "integer", "enum": [0, 1]}}, ["cell_id", "enabled"]),
    "estimate_neighbor_impact": _tool(
        "estimate_neighbor_impact", "For one proposed setting, how each neighbour's edge SINR and phones without service change.",
        {**_CELL, "parameter": {"type": "string", "enum": list(PARAMETERS)},
         "value": {"type": "integer", "description": "the new value"}}, ["cell_id", "parameter", "value"]),
    "run_risk_checks": _tool(
        "run_risk_checks",
        "The deterministic checks on one proposal: limits, protected cell, recent changes, evidence against the snapshot, "
        "a re-run of its simulation, neighbour impact and which specialists agree.",
        {"proposal_id": {"type": "string"}}, ["proposal_id"]),
    "load_skill": _tool(
        "load_skill", "Read a Dynatrace for AI skill: how Dynatrace stores problems, logs and spans, and how to query them in DQL.",
        {"name": {"type": "string", "enum": list(SKILLS)}}, ["name"]),
    "dt_query": _tool(
        "dt_query", "Run one read-only DQL query on Dynatrace with dtctl (fetch logs, spans, events or dt.davis.problems, at most "
        "2 hours back). Returns rows and a result_sha256 to cite.", {"dql": {"type": "string"}}, ["dql"]),
    "dt_log_patterns": _tool(
        "dt_log_patterns", "The most common network log lines of one kind in the last window_minutes, with counts.",
        {"kind": {"type": "string", "enum": ["cell", "phone", "tower", "safety", "step"]},
         "window_minutes": {"type": "integer"}}, ["kind"]),
    "dt_problems": _tool("dt_problems", "Open Dynatrace problems in the last window_minutes.",
                         {"window_minutes": {"type": "integer"}}, []),
}
READ = ["get_cell_state", "get_neighbor_state", "get_current_configuration", "get_recent_alarm_history"]
SIMULATE = ["simulate_power_change", "simulate_tilt_change", "estimate_neighbor_impact"]
DYNATRACE = ["dt_query", "dt_log_patterns", "dt_problems", "load_skill"]
AGENT_TOOLS = {
    "coverage": READ + SIMULATE + DYNATRACE,
    "capacity": READ + SIMULATE + DYNATRACE,
    "availability": READ + SIMULATE + ["simulate_cell_state"] + DYNATRACE,
    "risk": ["run_risk_checks", "estimate_neighbor_impact", "get_recent_alarm_history", "get_current_configuration", "dt_query"],
    "orchestrator": [],
}
OWNS = {"coverage": ("tx_power_dbm", "tilt_deg"), "capacity": ("tx_power_dbm", "tilt_deg"),
        "availability": ("enabled", "tx_power_dbm", "tilt_deg")}


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) or (isinstance(v, float) and v.is_integer())


def report_problems(report: dict) -> list:
    """What is wrong with a specialist's report (SpecialistReport); an empty list means it is valid."""
    problems = []
    if report.get("domain_verdict") not in VERDICTS:
        problems.append(f"domain_verdict must be one of {', '.join(VERDICTS)}")
    findings, proposals = report.get("findings", []), report.get("proposals", [])
    if not isinstance(findings, list) or not isinstance(proposals, list):
        return problems + ["findings and proposals must be lists"]
    for f in findings:
        if not isinstance(f, dict) or not f.get("cell_id") or not f.get("finding") or not isinstance(f.get("evidence", {}), dict):
            problems.append("each finding needs cell_id, finding and an evidence object")
            break
    if len(proposals) > 2:
        problems.append("at most 2 proposals")
    for p in proposals:
        if not isinstance(p, dict) or p.get("parameter") not in PARAMETERS or not _is_int(p.get("proposed_value")) \
                or not p.get("cell_id") or not isinstance(p.get("simulation_ref"), str):
            problems.append("each proposal needs cell_id, parameter (enabled|tx_power_dbm|tilt_deg), an integer "
                            "proposed_value and the simulation_ref from your simulation")
            break
    return problems


def assessments_problems(answer: dict, proposal_ids: list) -> list:
    """What is wrong with the risk critic's answer; every proposal needs one assessment."""
    got = {a.get("proposal_id"): a for a in answer.get("assessments") or [] if isinstance(a, dict)}
    problems = [f"no assessment for {pid}" for pid in proposal_ids if pid not in got]
    for a in got.values():
        if a.get("risk") not in RISKS or a.get("recommendation") not in RECOMMENDATIONS or not isinstance(a.get("reasons"), list):
            problems.append(f"{a.get('proposal_id')}: risk must be low|medium|high, recommendation one of "
                            f"{', '.join(RECOMMENDATIONS)}, reasons a list")
    return problems


def recommendation_problems(answer: dict, eligible_ids: list) -> list:
    if answer.get("chosen") is None and not eligible_ids:
        return []
    if answer.get("chosen") not in eligible_ids:
        return [f"chosen must be one of {', '.join(eligible_ids)}"]
    return [] if answer.get("rationale") else ["rationale is missing"]


class Refused(Exception):
    """A call the checkpoint did not allow."""
