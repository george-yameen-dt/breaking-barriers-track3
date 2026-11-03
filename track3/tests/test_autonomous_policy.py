"""The autonomous swarm policies (swarm.cedar) and the governance policies (governance.cedar).

Offline: Cedar only, no AWS. Each test names the @id that must decide, because those names are what the audit
records show and what the decision tree is traced by.
"""

import re
from pathlib import Path

import cedarpy
import pytest

from agents.governance import cedar_decide

POLICIES = Path(__file__).resolve().parent.parent / "policies"
NO_PERMIT = ["no policy permits this call"]
ORCH = 'Agent::"orchestrator"'
NETWORK = 'Network::"mobile-env-medium"'


def load(name: str) -> tuple:
    text = (POLICIES / name).read_text()
    return text, re.findall(r'@id\("([^"]+)"\)', text)


SWARM, SWARM_IDS = load("swarm.cedar")
GOV, GOV_IDS = load("governance.cedar")


def swarm(principal, action, resource, context=None, attrs=None):
    decision, policies, errors = cedar_decide(SWARM, SWARM_IDS, principal, action, resource, context or {}, attrs)
    assert not errors, errors  # a well-formed request never errors; an error would fail closed and hide the reason
    return decision, policies


def gov(principal, action, resource, **context):
    ctx = {"human_approved": False, "approver_role": "none", "checks_passed": True, "hash_matches": True, **context}
    decision, policies, errors = cedar_decide(GOV, GOV_IDS, principal, action, resource, ctx)
    assert not errors, errors
    return decision, policies


def apply_ctx(parameter="enabled", old=0, new=1, **over):
    return {"proposal_id": "PROP-availability-1", "parameter": parameter, "old_value": old, "new_value": new,
            "delta": new - old, "abs_delta": abs(new - old), "risk_checks_passed": True, "selected_proposal_matches": True,
            "applies_this_run": 0, "minutes_since_last_change": 9999, **over}


# ---- schemas: every policy is well-typed, so a misspelled context key is an error, not a silent never-permit -------------
@pytest.mark.parametrize("policy,schema", [("swarm.cedar", "swarm.cedarschema"),
                                           ("governance.cedar", "governance.cedarschema")])
def test_policies_validate_against_schema(policy, schema):
    result = cedarpy.validate_policies((POLICIES / policy).read_text(), (POLICIES / schema).read_text())
    assert result.validation_passed, [str(e) for e in result.errors]


def test_schema_catches_a_misspelled_context_key():
    broken = SWARM.replace("context.abs_delta <= 5", "context.abs_detla <= 5")
    result = cedarpy.validate_policies(broken, (POLICIES / "swarm.cedarschema").read_text())
    assert not result.validation_passed


def test_every_policy_has_an_id():
    assert len(SWARM_IDS) == len(re.findall(r"^(permit|forbid)\s*\(", SWARM, re.M))
    assert len(GOV_IDS) == len(re.findall(r"^(permit|forbid)\s*\(", GOV, re.M))


# ---- the decision tree for outage-C (tower C off), node by node ---------------------------------------------------------
def test_outage_C_decision_tree_ends_in_an_autonomous_change():
    tree = [
        (ORCH, "kpi_snapshot", NETWORK, {}, None, "snapshot-tool"),
        (ORCH, "handoff", 'Agent::"availability"', {}, None, "handoff-graph"),
        ('Agent::"availability"', "invoke_model", 'Model::"openai.gpt-oss-120b-1:0"', {}, {"status": "approved"},
         "models-approved-only"),
        ('Agent::"availability"', "get_cell_state", NETWORK, {}, None, "read-tools"),
        ('Agent::"availability"', "propose", 'Cell::"C"', {"parameter": "enabled", "proposal_id": "PROP-availability-1"},
         None, "domain-ownership"),
        (ORCH, "run_risk_checks", NETWORK, {}, None, "read-tools"),
        (ORCH, "select_proposal", 'Cell::"C"', {"proposal_id": "PROP-availability-1", "risk_checks_passed": True,
                                                "critic_agrees": True, "risk": "low"}, None, "select-eligible-only"),
        (ORCH, "apply_cell_configuration", 'Cell::"C"', apply_ctx(), None, "apply-autonomous-within-envelope"),
    ]
    for principal, action, resource, ctx, attrs, by in tree:
        assert swarm(principal, action, resource, ctx, attrs) == ("Allow", [by]), action


def test_no_human_approval_path_exists():
    assert swarm(ORCH, "request_human_approval", NETWORK) == ("Deny", NO_PERMIT)
    assert swarm(ORCH, "get_approval_status", NETWORK) == ("Deny", NO_PERMIT)


# ---- node: select ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("over", [{"risk_checks_passed": False}, {"critic_agrees": False}, {"risk": "high"}])
def test_select_refuses_ineligible(over):
    ctx = {"proposal_id": "P", "risk_checks_passed": True, "critic_agrees": True, "risk": "medium", **over}
    assert swarm(ORCH, "select_proposal", 'Cell::"C"', ctx) == ("Deny", NO_PERMIT)


def test_only_orchestrator_selects():
    ctx = {"proposal_id": "P", "risk_checks_passed": True, "critic_agrees": True, "risk": "low"}
    assert swarm('Agent::"coverage"', "select_proposal", 'Cell::"C"', ctx) == ("Deny", NO_PERMIT)


# ---- node: apply (the envelope) ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("parameter,old,new,expect", [
    ("tx_power_dbm", 40, 44, ("Allow", ["apply-autonomous-within-envelope"])),   # the demo fix: S4 40 -> 44 dBm
    ("tx_power_dbm", 38, 46, ("Allow", ["apply-autonomous-within-envelope"])),   # +8 dB is the largest step up
    ("tx_power_dbm", 40, 34, ("Allow", ["apply-autonomous-within-envelope"])),   # -6 dB is the largest step down
    ("tx_power_dbm", 40, 33, ("Deny", NO_PERMIT)),              # -7 dB: outside the envelope
    ("tx_power_dbm", 36, 45, ("Deny", NO_PERMIT)),              # +9 dB: outside the envelope
    ("tilt_deg", 4, 9, ("Allow", ["apply-autonomous-within-envelope"])),         # 5 degrees is the largest step
    ("tilt_deg", 4, 10, ("Deny", NO_PERMIT)),                   # 6 degrees: outside the envelope
    ("tilt_deg", 6, 3, ("Deny", ["tilt-range"])),               # tilt range is 4-10
    ("enabled", 0, 1, ("Allow", ["apply-autonomous-within-envelope"])),
    ("enabled", 1, 0, ("Deny", NO_PERMIT)),                     # switching a cell off is never autonomous
    ("tx_power_dbm", 44, 47, ("Deny", ["power-range"])),        # inside the step size, outside the range 30-46
    ("tx_power_dbm", 32, 29, ("Deny", ["power-range"])),
    ("tilt_deg", 9, 11, ("Deny", ["tilt-range"])),
])
def test_envelope(parameter, old, new, expect):
    assert swarm(ORCH, "apply_cell_configuration", 'Cell::"C"', apply_ctx(parameter, old, new)) == expect


@pytest.mark.parametrize("over,by", [
    ({"risk_checks_passed": False}, NO_PERMIT),
    ({"selected_proposal_matches": False}, NO_PERMIT),          # only the proposal that passed select
    ({"minutes_since_last_change": 0}, ["stabilization-15m"]),
    ({"applies_this_run": 1}, ["one-autonomous-change-per-run"]),
])
def test_apply_guards(over, by):
    assert swarm(ORCH, "apply_cell_configuration", 'Cell::"C"', apply_ctx(**over)) == ("Deny", by)


def test_second_apply_same_cell_in_15m_denied():
    assert swarm(ORCH, "apply_cell_configuration", 'Cell::"S4"', apply_ctx("tx_power_dbm", 40, 44, minutes_since_last_change=14)) == \
        ("Deny", ["stabilization-15m"])
    assert swarm(ORCH, "apply_cell_configuration", 'Cell::"S4"', apply_ctx("tx_power_dbm", 40, 44, minutes_since_last_change=15)) == \
        ("Allow", ["apply-autonomous-within-envelope"])


def test_power_above_46_denied():
    assert swarm(ORCH, "apply_cell_configuration", 'Cell::"S4"', apply_ctx("tx_power_dbm", 44, 50)) == ("Deny", ["power-range"])


def test_protected_cell_A():
    assert swarm(ORCH, "apply_cell_configuration", 'Cell::"A"', apply_ctx()) == ("Deny", ["protected-cell"])
    ctx = {"proposal_id": "P", "ledger_status": "VERIFY_FAILED", "restores_old_value": True}
    assert swarm(ORCH, "revert_cell_configuration", 'Cell::"A"', ctx) == ("Deny", ["protected-cell"])


def test_only_orchestrator_applies():
    assert swarm('Agent::"availability"', "apply_cell_configuration", 'Cell::"C"', apply_ctx()) == ("Deny", NO_PERMIT)


# ---- node: revert ------------------------------------------------------------------------------------------------------
def test_second_apply_in_run_denied_revert_allowed():
    assert swarm(ORCH, "apply_cell_configuration", 'Cell::"C"', apply_ctx(applies_this_run=1)) == \
        ("Deny", ["one-autonomous-change-per-run"])
    ctx = {"proposal_id": "PROP-availability-1", "ledger_status": "VERIFY_FAILED", "restores_old_value": True}
    assert swarm(ORCH, "revert_cell_configuration", 'Cell::"C"', ctx) == ("Allow", ["revert-exact"])


@pytest.mark.parametrize("over", [{"ledger_status": "VERIFIED"}, {"restores_old_value": False}])
def test_revert_only_after_failed_verify_and_exact(over):
    ctx = {"proposal_id": "P", "ledger_status": "VERIFY_FAILED", "restores_old_value": True, **over}
    assert swarm(ORCH, "revert_cell_configuration", 'Cell::"C"', ctx) == ("Deny", NO_PERMIT)


# ---- node: escalate ----------------------------------------------------------------------------------------------------
def test_orchestrator_may_ask_for_a_policy_change_but_nobody_else():
    ctx = {"proposal_id": "P", "denied_by": ["no policy permits this call"]}
    assert swarm(ORCH, "request_governance_change", 'Policy::"swarm"', ctx) == ("Allow", ["escalate-to-governance"])
    assert swarm('Agent::"coverage"', "request_governance_change", 'Policy::"swarm"', ctx) == ("Deny", NO_PERMIT)


# ---- the rest of the swarm set is unchanged ----------------------------------------------------------------------------
def test_withdrawn_model_refused():
    assert swarm('Agent::"capacity"', "invoke_model", 'Model::"openai.gpt-oss-20b-1:0"', {}, {"status": "withdrawn"}) == \
        ("Deny", NO_PERMIT)


def test_dql_write_refused():
    ctx = {"dql_verb": "fetch", "window_minutes": 60, "data_object": "logs", "has_write": True}
    assert swarm('Agent::"coverage"', "dt_query", NETWORK, ctx) == ("Deny", NO_PERMIT)


def test_specialist_cannot_hand_off():
    assert swarm('Agent::"coverage"', "handoff", 'Agent::"capacity"') == ("Deny", NO_PERMIT)


def test_capacity_cannot_switch_cells():
    assert swarm('Agent::"capacity"', "propose", 'Cell::"C"', {"parameter": "enabled"}) == ("Deny", NO_PERMIT)


# ---- governance: the one place a person says yes -----------------------------------------------------------------------
PERSON = 'Person::"aeric.walls"'


@pytest.mark.parametrize("action,resource", [("activate_policy", 'Policy::"swarm"'), ("activate_guardrail", 'Guardrail::"track3"')])
def test_risk_owner_activates(action, resource):
    assert gov(PERSON, action, resource, human_approved=True, approver_role="risk_owner") == \
        ("Allow", ["governance-change-needs-risk-owner-and-evidence"])


def test_before_approval_no_named_rule_refuses():
    # The flow asks Cedar first: with no approval yet, only "no permit" may come back, so asking a person can help.
    assert gov(PERSON, "activate_policy", 'Policy::"swarm"') == ("Deny", NO_PERMIT)


def test_agent_never_activates_even_with_risk_owner_approval():
    assert gov(ORCH, "activate_policy", 'Policy::"swarm"', human_approved=True, approver_role="risk_owner") == \
        ("Deny", ["agents-never-change-governance"])


@pytest.mark.parametrize("role", ["ai_engineer", "network_operator"])
def test_other_roles_cannot_approve(role):
    assert gov(PERSON, "activate_policy", 'Policy::"swarm"', human_approved=True, approver_role=role) == \
        ("Deny", ["only-risk-owner-approves-governance"])


def test_failed_checks_refused_whoever_approves():
    assert gov(PERSON, "activate_policy", 'Policy::"swarm"', human_approved=True, approver_role="risk_owner",
               checks_passed=False) == ("Deny", ["no-change-with-failed-checks"])


def test_approval_void_when_hash_changed():
    assert gov(PERSON, "activate_policy", 'Policy::"swarm"', human_approved=True, approver_role="risk_owner",
               hash_matches=False) == ("Deny", ["approval-bound-to-candidate-hash"])


def test_governance_is_not_self_amending():
    assert gov(PERSON, "activate_policy", 'Policy::"governance"', human_approved=True, approver_role="risk_owner") == \
        ("Deny", ["governance-is-not-self-amending"])
