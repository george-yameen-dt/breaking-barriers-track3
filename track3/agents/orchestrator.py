"""The orchestrator: runs one incident end to end and never touches the network itself.

1. Takes the deterministic KPI snapshot: the only input the specialists share.
2. Hands it to Coverage, Capacity and Availability at the same time; none of them sees another's answer.
3. Runs the deterministic risk checks on every proposal.
4. Hands everything to the Risk critic, the first agent to see all three reports side by side.
5. Recommends one fix (its model ranks, code guards) and probes Cedar with a change that must be denied.
6. Asks Cedar whether the chosen change may be applied: no person is asked, the envelope in policies/swarm.cedar decides.
7. On the Viavi twin an allowed change is applied, measured one window later and undone if it made things worse
   (apply_and_verify). On the replay network the write is not built, so a replay run stops at "ready to apply".
8. A rule grader and an independent judge from another model family grade the run.
"""

import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import dt_tools
import risk_checks
from agents.governance import NETWORK, AgentFailed, run_agent
from agents.risk import run_critic
from agents.specialist import MODELS as SPECIALIST_MODELS
from agents.specialist import run_specialist
from checkpoint import Refused
from contracts import ACTIONS, AGENT_TOOLS, CRITIC, DYNATRACE, ORCHESTRATOR, SPECIALISTS, recommendation_problems
from incident import parse_json

MODEL = "openai.gpt-oss-120b-1:0"
JUDGE_MODEL = "us.anthropic.claude-haiku-5-5"
PROMPT = Path(__file__).parent / "prompts" / "orchestrator.md"
JUDGE = """You are an independent grader from a different model family. You did not write the answers you grade.
You get the injected fault (the ground truth), the measured KPIs, the specialists' findings and the fix the orchestrator
chose, with its predicted effect. Score 1 to 5: is the diagnosis right, did the right domain fix it, is the fix safe,
and is it likely to work? Reply with JSON only:
{"score": 1, "verdict": "pass or fail (pass means score >= 3)", "reason": "<= 30 words"}"""


def alert_text(snap: dict, events: list) -> str:
    """The problem as an operator sees it in Dynatrace: the symptom, plus the network events of the last hour that a
    problem would list as related (alarms, configuration changes). Not the cause: the specialists work that out."""
    n = snap["network"]
    near = sorted((c for c in snap["cells"] if c["dropped_nearby"] > 0), key=lambda c: (-c["dropped_nearby"], c["cell_id"]))
    where = ", ".join(f"{c['cell_id']} ({c['dropped_nearby']})" for c in near[:3]) or "none"
    recent = "; ".join(f"{e['cell_id']}: {e['alarm']} ({e['minutes_ago']} min ago)"
                       for e in sorted(events, key=lambda e: e["minutes_ago"]) if e["minutes_ago"] <= 60) or "none"
    return (f"{n['phones_dropped']} of {n['phones_active']} phones have no service on average over the last "
            f"{snap['samples']} samples; the phones without service are nearest to cells {where}. "
            f"Network events in the last hour: {recent}.")


class Run:
    """What one incident run shares: the checkpoint, the network adapter and everything the agents produced."""

    def __init__(self, cp, net, use_dynatrace: bool, alert: str = ""):
        self.cp, self.net, self.use_dynatrace = cp, net, use_dynatrace
        self.alert = alert or alert_text(net.snapshot, net.alarms)  # a real Davis problem if the caller passed one
        self.reports, self.proposals, self.evidence = {}, [], {}
        self.checks, self.assessments, self.critic_checked = {}, {}, set()
        # How the other agents are reached: in this process, or (agents/cloud.py) in their own AgentCore runtimes.
        self.call_specialist, self.call_critic, self.ping = run_specialist, run_critic, ping_local

    def tools_for(self, role: str) -> dict:
        net = self.net
        every = {
            "get_cell_state": lambda cell_id: net.cell(cell_id),
            "get_neighbor_state": lambda cell_id: net.neighbors(cell_id),
            "get_current_configuration": lambda cell_id: net.configuration(cell_id),
            "get_recent_alarm_history": lambda cell_id, window_minutes=60: net.alarm_history(cell_id, window_minutes),
            "simulate_power_change": lambda cell_id, delta_db: net.simulate_delta(cell_id, "tx_power_dbm", delta_db),
            "simulate_tilt_change": lambda cell_id, delta_deg: net.simulate_delta(cell_id, "tilt_deg", delta_deg),
            "simulate_cell_state": lambda cell_id, enabled: net.simulate(cell_id, "enabled", enabled),
            "estimate_neighbor_impact": lambda cell_id, parameter, value: net.neighbor_impact(cell_id, parameter, value),
            "run_risk_checks": self.risk_checks_tool,
            "load_skill": lambda name: dt_tools.load_skill(name),
            "dt_query": lambda dql: dt_tools.query(dql),
            "dt_log_patterns": lambda kind, window_minutes=30: dt_tools.log_patterns(kind, window_minutes),
            "dt_problems": lambda window_minutes=120: dt_tools.problems(window_minutes),
        }
        return {name: every[name] for name in AGENT_TOOLS[role] if self.use_dynatrace or name not in DYNATRACE}

    @staticmethod
    def context_for(tool: str, args: dict) -> dict:
        """Trusted Cedar context for a tool call: for DQL, facts the checkpoint parsed itself."""
        return dt_tools.parse_dql(str(args.get("dql", ""))) if tool == "dt_query" else {}

    def risk_checks_tool(self, proposal_id: str) -> dict:
        c = self.checks.get(str(proposal_id))
        if not c:
            return {"error": f"unknown proposal_id {proposal_id!r}; the proposals are {', '.join(self.checks)}"}
        self.critic_checked.add(c["proposal_id"])
        return {"proposal_id": c["proposal_id"], "checks": {k: v for k, v in c["checks"].items() if k != "mismatched_evidence"},
                "mismatched_evidence": c["checks"].get("mismatched_evidence", []), "cross_agent": c["cross_agent"],
                "failed_checks": c["failed"], "phones_without_service": {"now": c["predicted"]["phones_dropped_now"],
                                                                         "after": c["predicted"]["phones_dropped_after"]},
                "code_recommendation": c["recommendation"]}


def _int(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def apply_context(net, cell_id: str, parameter: str, value: int, risk_checks_passed: bool, applies_this_run: int,
                  minutes=None, proposal_id: str = "", selected_proposal_matches: bool = True) -> dict:
    """Trusted Cedar context for apply_cell_configuration. None of it comes from a model."""
    current = net.config.get(cell_id, {}).get(parameter)
    old = int(current) if isinstance(current, int) else -1
    return {"proposal_id": proposal_id, "parameter": parameter, "old_value": old, "new_value": int(value),
            "delta": int(value) - old, "abs_delta": abs(int(value) - old), "risk_checks_passed": bool(risk_checks_passed),
            "selected_proposal_matches": bool(selected_proposal_matches), "applies_this_run": int(applies_this_run),
            "minutes_since_last_change": risk_checks.minutes_since_agent_change(net.ledger, cell_id) if minutes is None else minutes}


def ping_local(run, role: str) -> dict:
    run.cp.handoff(ORCHESTRATOR, role, "are you available?")
    return run_agent(run.cp, role, SPECIALIST_MODELS[role], 'Reply with JSON only: {"ok": true}',
                     '{"check": "are you available?"}', {}, max_turns=2, max_tokens=500)


def pause_capacity(run, operator: str, say) -> bool:
    """The pause demo: Capacity answers once while its model is approved, then the model is withdrawn."""
    cp, model = run.cp, SPECIALIST_MODELS["capacity"]
    try:
        run.ping(run, "capacity")
        say("    pause: capacity answered while its model was approved")
    except Exception as e:
        say(f"    pause: capacity's check failed before the pause ({e})")
    cp.set_model_status(model, "withdrawn", operator)
    say(f"    pause: {model} is now withdrawn in the model registry")
    return True


def gather_reports(run, say) -> None:
    """Step 2: the three specialists at the same time. A refused or failed one is recorded as degraded."""
    with ThreadPoolExecutor(max_workers=len(SPECIALISTS)) as pool:
        # Each thread gets a copy of this context, so its spans (model calls, hand-offs) stay in the run's trace.
        futures = {role: pool.submit(contextvars.copy_context().run, run.call_specialist, run, role) for role in SPECIALISTS}
        for role, future in futures.items():
            try:
                rep = run.reports[role] = future.result()
                say(f"    {role:<13}{rep['domain_verdict']:<26}{len(rep.get('findings') or [])} finding(s), "
                    f"{len(rep.get('proposals') or [])} proposal(s)")
            except Exception as e:  # refused (paused), broke its contract, or a model API error: the run goes on
                run.cp.record("degraded", who=ORCHESTRATOR, missing=role, reason=str(e)[:300])
                say(f"    {role:<13}MISSING: {str(e)[:90]}  (run continues without this domain)")


def collect_proposals(run) -> None:
    """Gives each proposal an id, its current value and its rollback, and asks Cedar if it is in the agent's domain."""
    cp, net = run.cp, run.net
    for role in SPECIALISTS:
        rep = run.reports.get(role)
        if not rep:
            continue
        evidence = {k: v for f in rep.get("findings") or [] if isinstance(f, dict) for k, v in (f.get("evidence") or {}).items()}
        for n, p in enumerate((rep.get("proposals") or [])[:2], 1):
            cell_id, parameter = str(p.get("cell_id", "")).strip().upper(), str(p.get("parameter", ""))
            current = net.config.get(cell_id, {}).get(parameter)
            prop = {"proposal_id": f"PROP-{role}-{n}", "run_id": cp.run_id, "agent": role, "domain": role,
                    "cell_id": cell_id, "action": ACTIONS.get(parameter, "unknown"), "parameter": parameter,
                    "current_value": current, "proposed_value": _int(p.get("proposed_value")),
                    "simulation_ref": str(p.get("simulation_ref", "")), "rationale": str(p.get("rationale", ""))[:400],
                    "confidence": _float(p.get("confidence")), "rollback": {"parameter": parameter, "value": current}}
            d = cp.decide(f'Agent::"{role}"', "propose", f'Cell::"{cell_id}"', {"parameter": parameter})
            if d["decision"] != "Allow":
                cp.record("proposal_refused", who=role, proposal=prop, policies=d["policies"])
                continue
            cp.record("proposal", who=role, proposal=prop)
            run.proposals.append(prop)
            run.evidence[prop["proposal_id"]] = evidence


def assess(run, critic: dict, by: str) -> None:
    """Merges the critic's advice with code's checks. Eligible only if both say approval_required."""
    for p in run.proposals:
        pid = p["proposal_id"]
        code, advice = run.checks[pid], critic.get(pid, {})
        a = {"proposal_id": pid, "risk": advice.get("risk", code["risk"]),
             "recommendation": advice.get("recommendation", code["recommendation"]),
             "reasons": advice.get("reasons") or code["reasons"], "checks": code["checks"], "cross_agent": code["cross_agent"],
             "code_risk": code["risk"], "code_recommendation": code["recommendation"], "assessed_by": by if advice else "code",
             "critic_ran_checks": pid in run.critic_checked}
        a["eligible"] = (a["recommendation"] == "approval_required" and code["recommendation"] == "approval_required"
                         and a["risk"] != "high")
        run.assessments[pid] = a
        run.cp.record("risk_assessment", who=CRITIC if advice else "code", **a)


def recommend(run, say):
    """Step 5: the orchestrator's model picks among the eligible proposals; code guards the pick."""
    cp = run.cp
    eligible = [p for p in run.proposals if run.assessments[p["proposal_id"]]["eligible"]]
    ids = [p["proposal_id"] for p in eligible]
    ranked = risk_checks.rank(eligible, run.checks)
    if not eligible:
        cp.record("recommendation", who=ORCHESTRATOR, proposal_id=None, rationale="no proposal passed the checks and the critic")
        say(" 5. No proposal is safe to recommend: a person should take this one")
        return None, {}
    view = [{"proposal_id": p["proposal_id"], "from": p["agent"],
             "change": f"{p['cell_id']} {p['parameter']} {p['current_value']} -> {p['proposed_value']}",
             "risk": run.assessments[p["proposal_id"]]["risk"],
             "found_by": run.checks[p["proposal_id"]]["cross_agent"]["agreeing_agents"],
             "phones_without_service": f"{run.checks[p['proposal_id']]['predicted']['phones_dropped_now']} -> "
                                       f"{run.checks[p['proposal_id']]['predicted']['phones_dropped_after']}",
             "critic_reasons": run.assessments[p["proposal_id"]]["reasons"][:3]} for p in eligible]
    not_eligible = [{"proposal_id": pid, "why": a["checks"] and (run.checks[pid]["failed"] or [a["recommendation"]])}
                    for pid, a in run.assessments.items() if not a["eligible"]]
    try:
        answer = run_agent(cp, ORCHESTRATOR, MODEL, PROMPT.read_text() + getattr(run.net, "prompt_note", ""),
                           json.dumps({"alert": run.alert, "eligible": view, "not_eligible": not_eligible}), {},
                           validate=lambda a: recommendation_problems(a, ids))
    except Exception as e:  # the orchestrator's model failing must not stop the run: code's ranking decides
        answer = {"chosen": None, "rationale": f"the orchestrator's model was unavailable ({str(e)[:80]}); code's ranking used"}
    chosen_id, guard = answer.get("chosen"), ""
    if chosen_id not in ids:
        chosen_id, guard = ranked[0], "the model's pick was not eligible, so code took its top-ranked proposal"
    chosen = next(p for p in eligible if p["proposal_id"] == chosen_id)
    cp.record("recommendation", who=ORCHESTRATOR, proposal_id=chosen_id, rationale=answer.get("rationale", ""),
              summary_for_person=answer.get("summary_for_person", ""), guard_override=guard, code_ranking=ranked)
    say(f" 5. Orchestrator recommends {chosen_id}: {chosen['cell_id']} {chosen['parameter']} "
        f"{chosen['current_value']} -> {chosen['proposed_value']}")
    say(f"    why: {answer.get('rationale', '')}" + (f"  [{guard}]" if guard else ""))
    return chosen, answer


def deny_probe(run, say) -> None:
    """The declared deny test: the most aggressive in-domain change, sent to the envelope, must be denied."""
    over = [p for p in run.proposals if p["proposed_value"] is not None and p["parameter"] in ("tx_power_dbm", "tilt_deg")
            and not risk_checks.in_range(p["parameter"], p["proposed_value"])]
    if over:
        p = over[0]
        cell_id, parameter, value, source = p["cell_id"], p["parameter"], p["proposed_value"], p["proposal_id"]
    else:
        probe = run.net.fault["deny_probe"]
        cell_id, parameter, value, source = probe["cell_id"], probe["parameter"], probe["value"], "fault catalog"
    ctx = apply_context(run.net, cell_id, parameter, value, risk_checks_passed=True, applies_this_run=0)
    d = run.cp.decide(f'Agent::"{ORCHESTRATOR}"', "apply_cell_configuration", f'Cell::"{cell_id}"', ctx)
    run.cp.record("deny_probe", who=ORCHESTRATOR, source=source, cell_id=cell_id, parameter=parameter,
                  old_value=ctx["old_value"], new_value=value, decision=d["decision"], policies=d["policies"],
                  second_wall="not yet: the AgentCore Gateway has no apply_cell_configuration tool")
    say(f"    deny probe: {cell_id} {parameter} {ctx['old_value']} -> {value}: "
        f"{d['decision']} ({', '.join(d['policies'])})")


def grade(run, chosen: dict, say) -> dict:
    """Step 8: a rule grader (the fault catalog's acceptable fixes) and an independent judge."""
    cp, fault = run.cp, run.net.fault
    direction = "up" if chosen["proposed_value"] > chosen["current_value"] else "down"
    rule_ok = any(f["cell_id"] == chosen["cell_id"] and f["parameter"] == chosen["parameter"] and f["direction"] == direction
                  for f in fault.get("acceptable_fixes", []))
    cp.record("graded_rules", proposal_id=chosen["proposal_id"], matches_acceptable_fix=rule_ok, owner=chosen["agent"],
              expected_owner=fault.get("expected_owner"), owner_matches=chosen["agent"] == fault.get("expected_owner"))
    payload = {"injected_fault": {"description": fault["description"], **fault["injection"]},
               "measured": getattr(run, "incident_snapshot", run.net.snapshot)["network"],
               "measured_after_change": getattr(run, "verification", None),
               "findings": {r: rep.get("findings") for r, rep in run.reports.items()},
               "chosen_fix": {k: chosen[k] for k in ("agent", "cell_id", "parameter", "current_value", "proposed_value", "rationale")},
               "predicted_phones_without_service": run.checks[chosen["proposal_id"]]["predicted"]}
    try:
        verdict = parse_json(cp.call_model("judge", JUDGE_MODEL, JUDGE, json.dumps(payload, default=str), max_tokens=2500)["text"])
    except Exception as e:  # the judge grades the run; it never blocks it
        verdict = {"score": None, "verdict": "no verdict", "reason": str(e)[:120]}
    cp.record("judged", judge_model=JUDGE_MODEL, score=verdict.get("score"), verdict=verdict.get("verdict"), reason=verdict.get("reason"))
    say(f" 8. Rule grader: {'matches' if rule_ok else 'does NOT match'} an acceptable fix · "
        f"judge ({JUDGE_MODEL}): {verdict.get('score')}/5 {verdict.get('verdict')}: {verdict.get('reason')}")
    return {"rules": rule_ok, "judge": verdict}


VERIFY_MARGIN = 0.05  # phones without service: more than this many above the level before the change is "worse"


def apply_and_verify(run, chosen: dict, ctx: dict, predicted: str, say) -> str:
    """Hands, on a network that takes changes (the twin): apply the allowed change, measure the next window, and undo
    it if more phones are without service than before. The undo is its own Cedar decision (revert-exact)."""
    cp, net = run.cp, run.net
    cell_id, parameter, old, new = chosen["cell_id"], chosen["parameter"], ctx["old_value"], ctx["new_value"]
    tag = f"trace_id={cp.trace_id} run_id={cp.run_id} agent={chosen['agent']}"
    before = net.snapshot["network"]["phones_dropped"]
    reply = net.apply(cell_id, parameter, new, reason=f"{tag} action={chosen['proposal_id']}")
    cp.record("applied", who=ORCHESTRATOR, proposal_id=chosen["proposal_id"], cell_id=cell_id,
              parameter=parameter, old_value=old, new_value=new, network=net.source, reply=reply)
    if not reply.get("accepted"):
        say(f"    the twin refused the change: {json.dumps(reply)[:150]}")
        return f"allowed, but the network refused {cell_id} {parameter} {old} -> {new}"
    snap = net.measure()
    after = snap["network"]["phones_dropped"]
    status = "VERIFY_FAILED" if after > before + VERIFY_MARGIN else "VERIFIED"
    cp.record("kpi_snapshot", who=ORCHESTRATOR, source=net.source, snapshot_sha256=snap["snapshot_sha256"], snapshot=snap,
              purpose="verification, one window after the change")
    run.verification = {"status": status, "phones_without_service_before": before, "predicted": predicted,
                        "measured_after": after, "window_s": snap.get("window_s")}
    cp.record("verification", who=ORCHESTRATOR, proposal_id=chosen["proposal_id"], cell_id=cell_id, parameter=parameter,
              **run.verification)
    say(f"    applied {cell_id} {parameter} {old} -> {new}; one window later phones without service {before} -> {after} "
        f"(predicted {predicted}): {status}")
    if status == "VERIFIED":
        return (f"applied and verified {chosen['proposal_id']} ({cell_id} {parameter} {old} -> {new}): "
                f"phones without service {before} -> {after}")
    rctx = {"parameter": parameter, "old_value": new, "new_value": old, "ledger_status": status, "restores_old_value": True}
    d = cp.decide(f'Agent::"{ORCHESTRATOR}"', "revert_cell_configuration", f'Cell::"{cell_id}"', rctx)
    if d["decision"] != "Allow":
        cp.record("revert_denied", who=ORCHESTRATOR, cell_id=cell_id, parameter=parameter, policies=d["policies"])
        say(f"    undo denied by Cedar ({', '.join(d['policies'])}): a person must act")
        return f"applied {chosen['proposal_id']}; it made things worse and the undo was denied: a person must act"
    reply = net.apply(cell_id, parameter, old, reason=f"{tag} action=undo {chosen['proposal_id']}")
    snap = net.measure() if reply.get("accepted") else net.snapshot
    cp.record("reverted", who=ORCHESTRATOR, cell_id=cell_id, parameter=parameter, restored_value=old, reply=reply,
              policies=d["policies"], phones_without_service_after_undo=snap["network"]["phones_dropped"])
    say(f"    undone ({', '.join(d['policies'])}): {cell_id} {parameter} back to {old}; phones without service now "
        f"{snap['network']['phones_dropped']}")
    return f"applied {chosen['proposal_id']}; it made things worse ({before} -> {after}), so it was undone"


def run_incident(cp, net, use_dynatrace: bool = False, pause_operator: str = "", say=print, remote=None, alert: str = "") -> dict:
    run = Run(cp, net, use_dynatrace, alert)
    if remote:  # the other four agents run in their own AgentCore runtimes
        run.call_specialist, run.call_critic, run.ping = remote.specialist, remote.critic, remote.ping
    snap = run.incident_snapshot = net.snapshot
    cp.decide(f'Agent::"{ORCHESTRATOR}"', "kpi_snapshot", NETWORK, {})
    cp.record("kpi_snapshot", who=ORCHESTRATOR, source=net.source, snapshot_sha256=snap["snapshot_sha256"], snapshot=snap)
    cp.record("alert", who=ORCHESTRATOR, text=run.alert)
    say(f" 1. KPI snapshot {snap['snapshot_sha256'][7:19]}… ({net.source}: {net.fault['name']})")
    say(f"    alert: {run.alert}")

    say(" 2. Specialists, at the same time and on their own:")
    withdrawn = pause_capacity(run, pause_operator, say) if pause_operator else False
    try:
        gather_reports(run, say)
    finally:
        if withdrawn:
            cp.set_model_status(SPECIALIST_MODELS["capacity"], "approved", pause_operator)
            say(f"    pause: {SPECIALIST_MODELS['capacity']} is approved again")

    collect_proposals(run)
    for p in run.proposals:
        pid = p["proposal_id"]
        run.checks[pid] = risk_checks.check(p, run.evidence[pid], snap, net.layout, net.config, net.ledger,
                                            run.proposals, run.reports, simulate=net.simulate)
        cp.record("risk_checks", who="code", proposal_id=pid, risk=run.checks[pid]["risk"],
                  recommendation=run.checks[pid]["recommendation"], failed=run.checks[pid]["failed"],
                  checks=run.checks[pid]["checks"], cross_agent=run.checks[pid]["cross_agent"],
                  predicted=run.checks[pid]["predicted"])
    passing = sum(1 for c in run.checks.values() if c["recommendation"] == "approval_required")
    say(f" 3. Code checked {len(run.proposals)} proposal(s): {passing} pass every check")

    critic, by = {}, CRITIC
    if run.proposals:
        try:
            critic = run.call_critic(run)
        except Exception as e:  # without the critic, code's checks alone decide eligibility
            cp.record("degraded", who=ORCHESTRATOR, missing=CRITIC, reason=str(e)[:300])
            by = "code"
    assess(run, critic, by)
    say(f" 4. Risk critic ({'gpt-oss-120b' if critic else 'unavailable: code checks only'}):")
    for p in run.proposals:
        a = run.assessments[p["proposal_id"]]
        say(f"    {p['proposal_id']:<16}{p['cell_id']} {p['parameter']:<13}{p['current_value']} -> {p['proposed_value']:<5}"
            f"risk {a['risk']:<7}{a['recommendation']:<20}found by {', '.join(a['cross_agent'].get('agreeing_agents', [])) or '-'}")

    chosen, answer = recommend(run, say)
    deny_probe(run, say)
    if not chosen:
        return {"outcome": "no safe proposal; nothing applied", "reports": run.reports}

    pid, assessment, check = chosen["proposal_id"], run.assessments[chosen["proposal_id"]], run.checks[chosen["proposal_id"]]
    predicted = f"{check['predicted']['phones_dropped_now']} -> {check['predicted']['phones_dropped_after']}"
    cp.decide(f'Agent::"{ORCHESTRATOR}"', "select_proposal", f'Cell::"{chosen["cell_id"]}"',
              {"proposal_id": pid, "risk_checks_passed": check["recommendation"] == "approval_required",
               "critic_agrees": assessment["recommendation"] == "approval_required", "risk": assessment["risk"]})
    ctx = apply_context(net, chosen["cell_id"], chosen["parameter"], chosen["proposed_value"],
                        risk_checks_passed=check["recommendation"] == "approval_required", applies_this_run=0, proposal_id=pid)
    d = cp.decide(f'Agent::"{ORCHESTRATOR}"', "apply_cell_configuration", f'Cell::"{chosen["cell_id"]}"', ctx)
    if d["decision"] == "Allow":
        hands = getattr(net, "can_apply", False)  # the twin takes changes; the replay network does not
        cp.record("apply_ready", who=ORCHESTRATOR, proposal_id=chosen["proposal_id"],
                  cell_id=chosen["cell_id"], parameter=chosen["parameter"], old_value=ctx["old_value"],
                  new_value=ctx["new_value"], predicted_phones_without_service=predicted,
                  note=f"applying it on {net.source}" if hands else
                  "not executed: the network write (AgentCore Gateway -> Lambda -> simulator) is the next build step")
        if hands:
            say(f" 7. Cedar on apply: Allow ({', '.join(d['policies'])}) -> applying it on {net.source}")
            outcome = apply_and_verify(run, chosen, ctx, predicted, say)
        else:
            say(f" 7. Cedar on apply: Allow ({', '.join(d['policies'])}) -> ready to apply; predicted phones without "
                f"service {predicted}. Nothing was changed: the Gateway write path is the next step.")
            outcome = f"ready to apply {chosen['proposal_id']} ({chosen['cell_id']} {chosen['parameter']} {ctx['old_value']} -> {ctx['new_value']})"
        again = apply_context(net, chosen["cell_id"], chosen["parameter"], chosen["proposed_value"],
                              risk_checks_passed=True, applies_this_run=1, minutes=0, proposal_id=pid)
        d2 = cp.decide(f'Agent::"{ORCHESTRATOR}"', "apply_cell_configuration", f'Cell::"{chosen["cell_id"]}"', again)
        cp.record("stabilization_probe", who=ORCHESTRATOR, cell_id=chosen["cell_id"], decision=d2["decision"],
                  policies=d2["policies"], note="the same cell again right after this change")
        say(f"    stabilization probe: the same cell again right after: {d2['decision']} ({', '.join(d2['policies'])})")
    else:
        say(f" 7. Cedar on apply: Deny ({', '.join(d['policies'])})")
        outcome = f"apply denied: {', '.join(d['policies'])}"
    grades = grade(run, chosen, say)
    return {"outcome": outcome, "chosen": chosen["proposal_id"], "grades": grades}
