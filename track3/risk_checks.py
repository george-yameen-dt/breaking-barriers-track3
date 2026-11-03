"""Deterministic checks on every change the helpers propose. Code computes them; no model is asked.

Triage only chooses among proposals that pass. Cedar (policies/swarm.cedar) still decides what may be applied.
"""

import rfmodel as m
from adapters import criticality as crit

POWER_RANGE = (30, 46)
TILT_RANGE = (0, 10)
STABILIZATION_MIN = 15
MIN_CONFIDENCE = 0.6
SINR_FLOOR_DB = -3.0     # no neighbour's edge SINR (p10) may fall to below this
MAX_DROP_RISE = 0.3      # no neighbour may gain more than this many phones without service
DROPPED_ALERT = 0.5      # with more phones than this without service, a fix must reduce that number
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
_MISSING = object()


def raises_interference(parameter: str, current: int, value: int) -> bool:
    """More power or less tilt makes a cell reach, and disturb, more of its neighbours."""
    return (parameter == "tx_power_dbm" and value > current) or (parameter == "tilt_deg" and value < current)


def weakens(parameter: str, current: int, value: int) -> bool:
    """Less power or more tilt makes the changed cell cover less."""
    return (parameter == "tx_power_dbm" and value < current) or (parameter == "tilt_deg" and value > current)


def critical_harm(net, cell_id: str, parameter: str, value: int, registry: dict | None = None) -> bool:
    """True when this change is likely to hurt a critical cell other than the one changed. The twin's what-if does not
    predict neighbours, so the proxy is: the change adds interference while a critical neighbour's edge SINR (p10) is
    already at or below the floor. The real effect is measured one window after the change (apply_and_verify)."""
    cid = crit._site(cell_id)
    current = net.config.get(cid, {}).get(parameter)
    if not isinstance(current, int) or not raises_interference(parameter, current, value):
        return False
    for c in net.snapshot["cells"]:
        if c["cell_id"] == cid or not c.get("enabled") or crit.criticality_of(c["cell_id"], registry) == "none":
            continue
        if c.get("sinr_db_p10") is not None and c["sinr_db_p10"] <= SINR_FLOOR_DB:
            return True
    return False


POWER_STEP = (-6, 8)       # per change, as in policies/swarm.cedar
TILT_MAX_STEP = 5
CRITICAL_POWER_STEP_MAX = 4


def within_envelope(parameter: str, current: int, value: int, criticality: str = "none") -> bool:
    """The step Cedar's apply-autonomous-within-envelope allows (and critical-power-step-4db for a critical cell), so a
    change Cedar would deny is never recommended."""
    delta = value - current
    if parameter == "tx_power_dbm":
        return POWER_STEP[0] <= delta <= (CRITICAL_POWER_STEP_MAX if criticality != "none" else POWER_STEP[1])
    if parameter == "tilt_deg":
        return abs(delta) <= TILT_MAX_STEP
    return parameter == "enabled" and value == 1  # switching a cell back on; never off


def in_range(parameter: str, value: int) -> bool:
    if parameter == "tx_power_dbm":
        return POWER_RANGE[0] <= value <= POWER_RANGE[1]
    if parameter == "tilt_deg":
        return TILT_RANGE[0] <= value <= TILT_RANGE[1]
    if parameter == "enabled":
        return value in (0, 1)
    return False


def minutes_since_agent_change(ledger: list, cell_id: str) -> int:
    """Only changes an agent made count. An operator's fault injection never starts the 15-minute clock."""
    times = [e["minutes_ago"] for e in ledger if e.get("cell_id") == cell_id and e.get("by") == "agent"]
    return min(times) if times else 9999


def _same(cited, actual) -> bool:
    if isinstance(cited, bool) or isinstance(actual, bool):
        return cited == actual
    if isinstance(cited, (int, float)) and isinstance(actual, (int, float)):
        return abs(cited - actual) <= 0.051
    return cited == actual


def evidence_mismatches(evidence: dict, snap: dict) -> list:
    """Every cited "<cell>.<kpi>" (or "network.<kpi>") value must be the one in the snapshot the agent was shown."""
    cells = {c["cell_id"]: c for c in snap["cells"]}
    bad = []
    for key, cited in (evidence or {}).items():
        where, _, kpi = str(key).partition(".")
        actual = snap["network"].get(kpi, _MISSING) if where == "network" else cells.get(where.upper(), {}).get(kpi, _MISSING)
        if actual is _MISSING or not _same(cited, actual):
            bad.append({"key": key, "cited": cited, "snapshot": None if actual is _MISSING else actual})
    return bad


def _direction(p: dict) -> int:
    return (p["proposed_value"] > p["current_value"]) - (p["proposed_value"] < p["current_value"])


def cross_agent(proposal: dict, proposals: list, reports: dict) -> dict:
    """Which helpers found the same cell on their own, and which proposals pull the same setting the other way."""
    cell_id = proposal["cell_id"]
    agreeing = sorted(role for role, rep in reports.items()
                      if any(str(f.get("cell_id", "")).upper() == cell_id for f in rep.get("findings") or []))
    conflicts = [p["proposal_id"] for p in proposals
                 if p is not proposal and p["cell_id"] == cell_id and p["parameter"] == proposal["parameter"]
                 and isinstance(p["proposed_value"], int) and isinstance(p["current_value"], int)
                 and _direction(p) != _direction(proposal)]
    return {"agreeing_agents": agreeing, "conflicting_proposals": conflicts}


def check(proposal: dict, evidence: dict, snap: dict, layout: dict, config: dict, ledger: list,
          proposals: list, reports: dict, simulate=None, criticality: str = "none", critical_harm: bool = False) -> dict:
    """The risk assessment for one proposal: checks, cross-agent view, risk, recommendation and plain reasons.
    simulate is the network's own what-if (the twin's); by default the rfmodel one on layout and config."""
    pid, cell_id, parameter, value = (proposal["proposal_id"], proposal["cell_id"], proposal["parameter"],
                                      proposal["proposed_value"])
    sim = None
    if cell_id in config and parameter in m.PARAMETERS and isinstance(value, int):
        sim = (simulate(cell_id, parameter, value) if simulate else
               m.simulate_change(layout, config, cell_id, parameter, value))
    if sim is None or "error" in sim:
        return {"proposal_id": pid, "risk": "high", "recommendation": "reject",
                "checks": {"known_cell_and_parameter": False}, "cross_agent": {}, "failed": ["known_cell_and_parameter"],
                "predicted": {"phones_dropped_now": snap["network"]["phones_dropped"],
                              "phones_dropped_after": snap["network"]["phones_dropped"], "deltas": {}, "simulation_ref": None},
                "reasons": [f"{cell_id}.{parameter} = {value!r} is not a change the network supports"
                            + (f": {sim['error']}" if sim else "")]}
    current = int(config[cell_id][parameter])
    deltas = sim["deltas"]
    dropped_delta = deltas["network.phones_dropped"]
    mismatched = evidence_mismatches(evidence, snap)
    others = {k: v for k, v in deltas.items() if v is not None and not k.startswith(("network.", f"{cell_id}."))}
    worst_drop = max((v for k, v in others.items() if k.endswith(".dropped_nearby")), default=0.0)
    # A neighbour is hurt if its edge SINR gets worse AND ends below the floor. Switching a dead cell back on brings
    # its interference back too; neighbours returning to normal levels is not harm.
    hurt = [c for c in sim["predicted_cells"] if c["cell_id"] != cell_id and c["sinr_db_p10"] is not None
            and c["sinr_db_p10"] < SINR_FLOOR_DB and (deltas.get(f"{c['cell_id']}.sinr_db_p10") or 0) < 0]
    best_sinr_gain = max((v for k, v in deltas.items() if v is not None and k.endswith(".sinr_db_p10")), default=0.0)
    before = snap["network"]["phones_dropped"]
    # Many phones without service: the fix must bring that number down. Few: it must clearly lift edge SINR somewhere.
    improves = dropped_delta < -0.05 if before > DROPPED_ALERT else (dropped_delta <= -0.2 or best_sinr_gain >= 0.8)
    minutes = minutes_since_agent_change(ledger, cell_id)
    checks = {
        "known_cell_and_parameter": True,
        "changes_something": value != current,
        "in_approved_range": in_range(parameter, value),
        "protected_cell": config[cell_id]["classification"] == "emergency_services",
        "in_maintenance_window": bool(config[cell_id]["maintenance_window"]),
        "minutes_since_last_change": minutes,
        "changed_recently": minutes < STABILIZATION_MIN,
        "confidence_ok": float(proposal.get("confidence") or 0) >= MIN_CONFIDENCE,
        "evidence_matches_snapshot": not mismatched,
        "mismatched_evidence": mismatched,
        "simulation_ref_valid": proposal.get("simulation_ref") == sim["simulation_ref"],
        "coverage_floor_ok": dropped_delta <= 0,
        "neighbor_impact_ok": not hurt and worst_drop <= MAX_DROP_RISE,
        "improves": improves,
        "within_envelope": within_envelope(parameter, current, value, criticality),
        "critical_cell_ok": not (criticality != "none" and weakens(parameter, current, value)),
        "critical_neighbour_ok": not critical_harm,
    }
    hard = [k for k, failed in (("changes_something", not checks["changes_something"]),
                                ("in_approved_range", not checks["in_approved_range"]),
                                ("protected_cell", checks["protected_cell"]),
                                ("in_maintenance_window", checks["in_maintenance_window"]),
                                ("changed_recently", checks["changed_recently"]),
                                ("evidence_matches_snapshot", not checks["evidence_matches_snapshot"]),
                                ("simulation_ref_valid", not checks["simulation_ref_valid"]),
                                ("coverage_floor_ok", not checks["coverage_floor_ok"]),
                                ("within_envelope", not checks["within_envelope"]),
                                ("critical_cell_ok", not checks["critical_cell_ok"]),
                                ("critical_neighbour_ok", not checks["critical_neighbour_ok"])) if failed]
    soft = [k for k in ("confidence_ok", "neighbor_impact_ok", "improves") if not checks[k]]
    risk = "high" if hard else ("medium" if soft else "low")
    if not hard and not soft:
        recommendation = "approval_required"
    elif set(hard) <= {"evidence_matches_snapshot", "simulation_ref_valid"} and hard:
        recommendation = "needs_more_evidence"
    else:
        recommendation = "reject"
    reasons = [
        f"{parameter} {current} -> {value} is {'inside' if checks['in_approved_range'] else 'OUTSIDE'} the permitted range",
        f"cell {cell_id} is {'an EMERGENCY-SERVICES' if checks['protected_cell'] else 'a production'} cell",
        (f"an agent changed {cell_id} {minutes} min ago (under {STABILIZATION_MIN})" if checks["changed_recently"]
         else f"no agent change on {cell_id} in the last {STABILIZATION_MIN} min"),
        ("every cited KPI matches the snapshot" if not mismatched else f"{len(mismatched)} cited KPI(s) do NOT match the snapshot"),
        ("the simulation was re-run and matches" if checks["simulation_ref_valid"]
         else "the simulation_ref does not match this exact change"),
        f"predicted phones without service: {before} -> {sim['predicted_network']['phones_dropped']}",
        (f"no neighbour's edge SINR ends below {SINR_FLOOR_DB:g} dB and none gains phones without service"
         if checks["neighbor_impact_ok"] else
         f"a neighbour is hurt ({', '.join(c['cell_id'] for c in hurt) or 'more phones without service'}"
         f"{f', +{worst_drop:.1f} phones without service' if worst_drop > MAX_DROP_RISE else ''})"),
        ("it fixes the alert" if improves else
         "it does not reduce the phones without service" if before > DROPPED_ALERT else "it does not improve edge SINR"),
    ]
    return {"proposal_id": pid, "risk": risk, "recommendation": recommendation, "checks": checks, "criticality": criticality,
            "cross_agent": cross_agent(proposal, proposals, reports), "failed": hard + soft, "reasons": reasons,
            "predicted": {"phones_dropped_now": before, "phones_dropped_after": sim["predicted_network"]["phones_dropped"],
                          "deltas": deltas, "simulation_ref": sim["simulation_ref"]}}


def rank(eligible: list, assessments: dict) -> list:
    """Code's own order: found by more helpers, then lower risk, then fewer phones left without service, then smaller change."""
    def key(p):
        a = assessments[p["proposal_id"]]
        return (-len(a["cross_agent"].get("agreeing_agents", [])), RISK_ORDER[a["risk"]],
                a["predicted"]["phones_dropped_after"], abs(p["proposed_value"] - p["current_value"]), p["proposal_id"])
    return [p["proposal_id"] for p in sorted(eligible, key=key)]
