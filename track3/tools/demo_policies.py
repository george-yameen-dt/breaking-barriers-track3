"""The Cedar policies, as a demo: real requests against the real policy files, no AWS.

    python3 tools/demo_policies.py            all three acts
    python3 tools/demo_policies.py --act 2    one act
    python3 tools/demo_policies.py --step     wait for Enter between scenarios (for a live audience)

Act 1: what the swarm may do alone (policies/swarm.cedar).  Act 2: who may change the rules (policies/governance.cedar).
Act 3: who may change the agents (policies/promotion.cedar).
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from policy import PolicySet, critical_power_step  # noqa: E402

STEP = critical_power_step()  # the critical-cell power step in the policy today

ORCH, COVERAGE, CAPACITY = 'Agent::"orchestrator"', 'Agent::"coverage"', 'Agent::"capacity"'
S4, S2, S3 = 'Cell::"S4"', 'Cell::"S2"', 'Cell::"S3"'
NO_PERMIT = "no policy permits this call"


def apply_ctx(parameter="tx_power_dbm", old=40, new=44, **over):
    return {"proposal_id": "PROP-demo", "parameter": parameter, "old_value": old, "new_value": new, "delta": new - old,
            "abs_delta": abs(new - old), "risk_checks_passed": True, "selected_proposal_matches": True, "applies_this_run": 0,
            "minutes_since_last_change": 9999, "criticality": "none", "critical_harm_predicted": False, **over}


def viavi(command_type="tx_power", trace=True, **over):
    return {"command_type": command_type, "delay_s": 0, "reason_has_trace_id": trace, **over}


def gov(**over):
    return {"human_approved": True, "approver_role": "risk_owner", "checks_passed": True, "hash_matches": True, **over}


def promo(**over):
    return {"human_approved": True, "approver_role": "ai_engineer", "eval_passed": True, "open_blocking_issues": 0, **over}


# (act, title, why it matters, policy set, principal, action, resource, context, attrs, expected decision, expected deciding policy)
SCENARIOS = [
    (1, "S4 power 40 -> 44 dBm", "The fix we ran live: a small step, inside the envelope, no person asked.", "swarm", ORCH,
     "apply_cell_configuration", S4, apply_ctx(), None, "Allow", "apply-autonomous-within-envelope"),
    (1, "Any cell to 50 dBm", "Hard ceiling: transmit power is 30-46 dBm, whatever anyone proposes.", "swarm", ORCH,
     "apply_cell_configuration", 'Cell::"S1"', apply_ctx(old=44, new=50), None, "Deny", "power-range"),
    (1, f"Emergency cell S4: +{STEP} dB (the largest step it gets)", f"A critical cell may gain power, in steps of at most +{STEP} dB.",
     "swarm", ORCH, "apply_cell_configuration", S4, apply_ctx(old=40, new=40 + STEP, criticality="emergency"), None, "Allow",
     "apply-autonomous-within-envelope"),
    (1, f"Emergency cell S4: +{STEP + 1} dB", f"A critical cell may gain power, but in smaller steps (at most +{STEP} dB).", "swarm", ORCH,
     "apply_cell_configuration", S4, apply_ctx(old=40, new=41 + STEP, criticality="emergency"), None, "Deny", "critical-power-step"),
    (1, "Hospital cell S2: cut power", "Never weaken a hospital, an emergency or a military cell.", "swarm", ORCH,
     "apply_cell_configuration", S2, apply_ctx(old=40, new=38, criticality="hospital"), None, "Deny", "critical-no-power-cut"),
    (1, "A fix that would hurt the hospital next door", "The checks predict harm to a critical neighbour: refused.", "swarm", ORCH,
     "apply_cell_configuration", S4, apply_ctx(critical_harm_predicted=True), None, "Deny", "critical-neighbour-no-harm"),
    (1, "Second change to S4 five minutes later", "Let the network settle: one change per cell per 15 minutes.", "swarm", ORCH,
     "apply_cell_configuration", S4, apply_ctx(minutes_since_last_change=5), None, "Deny", "stabilization-15m"),
    (1, "A second change in the same run", "One autonomous change per run: a blast-radius limit.", "swarm", ORCH,
     "apply_cell_configuration", S3, apply_ctx(applies_this_run=1), None, "Deny", "one-autonomous-change-per-run"),
    (1, "A specialist tries to apply a change", "Agents propose; only the orchestrator applies.", "swarm", CAPACITY,
     "apply_cell_configuration", S4, apply_ctx(), None, "Deny", NO_PERMIT),
    (1, "Undo a change that made things worse", "A revert is allowed, but only back to the exact old value and only after a failed check.",
     "swarm", ORCH, "revert_cell_configuration", S4,
     {"proposal_id": "PROP-demo", "ledger_status": "VERIFY_FAILED", "restores_old_value": True}, None, "Allow", "revert-exact"),
    (1, "A 'revert' that was never needed", "No failed verification, no undo.", "swarm", ORCH, "revert_cell_configuration", S4,
     {"proposal_id": "PROP-demo", "ledger_status": "VERIFIED", "restores_old_value": True}, None, "Deny", NO_PERMIT),
    (1, "Viavi: send tx_power with the run's trace id", "The one write the orchestrator has, and the audit trail can follow it.", "swarm",
     ORCH, "viavi_command", 'Tool::"viavi:simulation_command"', viavi(), {"status": "approved"}, "Allow", "viavi-command-allowlist"),
    (1, "Viavi: the same command with no trace id", "Every command carries the run's trace id, or it is not sent.", "swarm", ORCH,
     "viavi_command", 'Tool::"viavi:simulation_command"', viavi(trace=False), {"status": "approved"}, "Deny",
     "viavi-trace-id-in-reason"),
    (1, "Viavi: switch a cell off", "Only tx_power and antenna_tilt exist for the agents. raw, turn_off and handovers do not.", "swarm",
     ORCH, "viavi_command", 'Tool::"viavi:simulation_command"', viavi("turn_off"), {"status": "approved"}, "Deny", NO_PERMIT),
    (1, "A tool nobody approved", "Nothing is callable until it is registered and approved.", "swarm", ORCH, "viavi_read",
     'Tool::"viavi:simulation_status"', {}, {"status": "unregistered"}, "Deny", "tools-approved-only"),
    (1, "A withdrawn model is called (the pause)", "Withdraw a model in the registry and its calls are refused.", "swarm", CAPACITY,
     "invoke_model", 'Model::"openai.gpt-oss-20b-1:0"', {}, {"status": "withdrawn"}, "Deny", NO_PERMIT),
    (2, "An agent tries to change a policy", "No self-governance: an agent never changes the rules, its own envelope least of all.",
     "governance", ORCH, "activate_policy", 'Policy::"swarm"', gov(human_approved=False), None, "Deny", "agents-never-change-governance"),
    (2, "An AI engineer approves a policy change", "Separation of duties: only a risk owner approves changes to the rules.",
     "governance", 'Person::"alex"', "activate_policy", 'Policy::"swarm"', gov(approver_role="ai_engineer"), None, "Deny",
     "only-risk-owner-approves-governance"),
    (2, "The candidate policy fails its checks", "A candidate that does not validate, or drops a safety rule, is never activated.",
     "governance", 'Person::"riley"', "activate_policy", 'Policy::"swarm"', gov(checks_passed=False), None, "Deny",
     "no-change-with-failed-checks"),
    (2, "The file was edited after the approval", "The approval is for one exact file (its SHA-256): edit it and the approval is void.",
     "governance", 'Person::"riley"', "activate_policy", 'Policy::"swarm"', gov(hash_matches=False), None, "Deny",
     "approval-bound-to-candidate-hash"),
    (2, "A risk owner approves the exact, validated candidate", "The only way in: a named person, the exact file, the checks passed.",
     "governance", 'Person::"riley"', "activate_policy", 'Policy::"swarm"', gov(), None, "Allow",
     "governance-change-needs-risk-owner-and-evidence"),
    (2, "Someone tries to rewrite the governance rules themselves", "The rules about changing rules are changed only by code review in Git.",
     "governance", 'Person::"riley"', "activate_policy", 'Policy::"governance"', gov(), None, "Deny", "governance-is-not-self-amending"),
    (3, "An agent promotes its own new prompt", "No self-approval: an agent never promotes anything, itself included.", "promotion",
     ORCH, "promote", 'Prompt::"network-agent@v2"', promo(human_approved=False), {"sha256": "x"}, "Deny", "agents-never-promote"),
    (3, "A network operator approves a change to the agents", "Operators approve network changes, not changes to the agents.", "promotion",
     'Person::"sam"', "promote", 'Prompt::"network-agent@v2"', promo(approver_role="network_operator"), {"sha256": "x"}, "Deny",
     "operators-cannot-approve-agent-changes"),
    (3, "A candidate that missed its declared target", "The target is declared before the experiment: miss it and nothing goes live.",
     "promotion", 'Person::"alex"', "promote", 'Prompt::"network-agent@v2"', promo(eval_passed=False), {"sha256": "x"}, "Deny",
     "no-promotion-below-target"),
    (3, "An open critical issue in Arize Signal", "While a critical or high issue is open, nothing new goes live.", "promotion",
     'Person::"alex"', "promote", 'Prompt::"network-agent@v2"', promo(open_blocking_issues=1), {"sha256": "x"}, "Deny",
     "no-promotion-with-open-signal-issue"),
    (3, "An AI engineer approves, evidence holds", "Approved by the right role, target met, no open issue: it goes live.", "promotion",
     'Person::"alex"', "promote", 'Prompt::"network-agent@v2"', promo(), {"sha256": "x"}, "Allow",
     "promote-needs-ai-engineer-and-evidence"),
    (3, "The improvement loop tries to edit a policy", "Self-improvement changes how the swarm thinks, never what it may do.",
     "promotion", 'Loop::"improvement"', "activate_policy", 'Policy::"swarm"', {}, None, "Deny", "loop-never-touches-policy"),
]
ACTS = {1: "ACT 1  What the swarm may do alone   (policies/swarm.cedar)",
        2: "ACT 2  Who may change the rules      (policies/governance.cedar)",
        3: "ACT 3  Who may change the agents     (policies/promotion.cedar)"}


def run_all(acts=(1, 2, 3)) -> list:
    sets = {n: PolicySet(ROOT / "policies" / f"{n}.cedar") for n in ("swarm", "governance", "promotion")}
    out = []
    for act, title, why, pset, principal, action, resource, ctx, attrs, expect, expect_policy in SCENARIOS:
        if act not in acts:
            continue
        decision, policies, errors = sets[pset].evaluate(principal, action, resource, ctx, attrs)
        out.append({"act": act, "title": title, "why": why, "set": pset, "principal": principal, "action": action,
                    "decision": decision, "policies": policies, "errors": errors, "expect_decision": expect,
                    "expect_policy": expect_policy, "ok": decision == expect and expect_policy in policies and not errors})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--act", type=int, choices=(1, 2, 3))
    ap.add_argument("--step", action="store_true", help="wait for Enter between scenarios")
    a = ap.parse_args()
    results = run_all((a.act,) if a.act else (1, 2, 3))
    act = None
    for r in results:
        if r["act"] != act:
            act = r["act"]
            print(f"\n{ACTS[act]}\n" + "-" * 78)
        mark = "ALLOW" if r["decision"] == "Allow" else "DENY "
        print(f"\n  {mark}  {r['title']}\n         {r['why']}\n         decided by: {', '.join(r['policies'])}"
              + ("" if r["ok"] else f"   <-- EXPECTED {r['expect_decision']} by {r['expect_policy']}"))
        if a.step:
            input("\n  [Enter] ")
    bad = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(bad)} of {len(results)} scenarios behave as explained.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
