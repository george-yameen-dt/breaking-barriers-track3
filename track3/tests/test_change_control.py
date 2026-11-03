"""Changing the swarm's policy: validate, invariants, impact preview, a risk owner's approval of the exact file, activation. And the
runtime pin: a swarm run refuses to start under a policy nobody approved."""

import re
from pathlib import Path

import pytest

import change_control as cc
from checkpoint import Checkpoint, Refused

POLICIES = Path(__file__).resolve().parent.parent / "policies"
CURRENT = (POLICIES / "swarm.cedar").read_text()
SCHEMA = (POLICIES / "swarm.cedarschema").read_text()
TIGHTER = CURRENT.replace("context.delta > 4", "context.delta > 3")           # critical power step: +4 dB becomes +3 dB
NO_POWER_RANGE = re.sub(r'@id\("power-range"\)\nforbid[^;]*;', "", CURRENT)      # a candidate that drops a safety rule


class Registry:
    """A dict standing in for the DynamoDB registry table."""
    def __init__(self):
        self.items = {}

    def put_item(self, TableName, Item):
        if "model_id" in Item:  # the registry table; the audit records go to another table and are ignored here
            self.items[Item["model_id"]["S"]] = Item

    def get_item(self, TableName, Key):
        item = self.items.get(Key["model_id"]["S"])
        return {"Item": item} if item else {}


@pytest.fixture
def cp(offline_cp):
    offline_cp.ddb = Registry()
    return offline_cp


def approve_as(role, who="riley", sha=None, ok=True):
    def ask(info):
        return who, role, ok, "test", sha or info["candidate_sha256"]
    return ask


def request(cp, text, approve, by='Person::"alex"', stored=None):
    stored = stored if stored is not None else {}
    return cc.request_activation(cp, text, requested_by=by, approve=approve, store=lambda sha, body: stored.update({sha: body}), current=CURRENT)


def kinds(cp):
    import json
    return [json.loads(line) for line in cp.local.read_text().splitlines()]


# ---- the checks -----------------------------------------------------------------------------------
def test_a_candidate_that_validates_passes():
    assert cc.validate_candidate(TIGHTER, SCHEMA) == (True, [])


def test_a_syntax_error_is_refused_with_the_parsers_message():
    ok, errors = cc.validate_candidate(CURRENT + "\npermit (principal, action", SCHEMA)
    assert ok is False and errors


def test_a_misspelled_context_key_is_refused_by_the_schema():
    ok, errors = cc.validate_candidate(CURRENT.replace("context.delta > 4", "context.detla > 4"), SCHEMA)
    assert ok is False and errors


def test_a_candidate_that_drops_a_safety_rule_fails_the_invariants():
    ok, missing = cc.invariants(NO_POWER_RANGE)
    assert ok is False and missing == ["power-range"]
    assert cc.invariants(TIGHTER) == (True, [])


def test_the_impact_preview_lists_the_decisions_that_change():
    changed = cc.impact_preview(CURRENT, TIGHTER)
    assert [c["before"] + ">" + c["after"] for c in changed] == ["Allow>Deny"], changed
    assert cc.impact_preview(CURRENT, CURRENT) == []


# ---- the flow ----------------------------------------------------------------------------------------
def test_an_agent_request_is_denied_without_asking_anyone(cp):
    asked = []
    out = request(cp, TIGHTER, lambda info: asked.append(info), by='Agent::"orchestrator"')
    assert out["kind"] == "denied" and out["policies"] == ["agents-never-change-governance"] and not asked


def test_an_ai_engineer_cannot_approve_a_policy_change(cp):
    out = request(cp, TIGHTER, approve_as("ai_engineer"))
    assert out["kind"] == "denied" and out["policies"] == ["only-risk-owner-approves-governance"]


def test_failed_checks_are_refused_even_when_a_risk_owner_would_approve(cp):
    asked = []
    out = request(cp, NO_POWER_RANGE, lambda info: asked.append(info) or approve_as("risk_owner")(info))
    assert out["kind"] == "denied" and "no-change-with-failed-checks" in out["policies"] and not asked, "nobody is asked to approve it"


def test_an_approval_of_a_different_file_is_void(cp):
    out = request(cp, TIGHTER, approve_as("risk_owner", sha="sha256:" + "0" * 64))
    assert out["kind"] == "denied" and out["policies"] == ["approval-bound-to-candidate-hash"]
    assert cp.ddb.items == {}, "nothing was pinned"


def test_a_declined_approval_changes_nothing(cp):
    out = request(cp, TIGHTER, approve_as("risk_owner", ok=False))
    assert out["kind"] == "denied" and out["policies"] == ["person declined"] and cp.ddb.items == {}


def test_a_risk_owner_approving_the_exact_validated_file_activates_it(cp):
    stored = {}
    out = request(cp, TIGHTER, approve_as("risk_owner"), stored=stored)
    sha = cc.sha256_text(TIGHTER)
    assert out["kind"] == "policy_activated" and out["sha256"] == sha and stored == {sha: TIGHTER}
    assert cp.policy_pin("swarm") == sha
    ks = [r["kind"] for r in kinds(cp)]
    assert ks.index("policy_validation") < ks.index("policy_impact") < ks.index("approval_requested") < ks.index("approval") < ks.index("policy_activated")


def test_the_governance_rules_themselves_cannot_be_amended(cp):
    out = cc.request_activation(cp, "// anything", requested_by='Person::"alex"', approve=approve_as("risk_owner"), policy="governance",
                                store=lambda *a: None, current="")
    assert out["kind"] == "denied" and out["policies"] == ["governance-is-not-self-amending"]


# ---- the runtime pin: a swarm run refuses a policy nobody approved -------------------------------------------
def test_a_run_with_no_pin_yet_notes_it_and_goes_on(cp):
    assert cc.check_policy_pin(cp, CURRENT) is None
    assert kinds(cp)[-1]["kind"] == "policy_pin_checked" and kinds(cp)[-1]["pinned_sha256"] is None


def test_a_run_whose_policy_is_the_pinned_one_goes_on(cp):
    cp.set_policy_pin("swarm", cc.sha256_text(CURRENT), "riley")
    assert cc.check_policy_pin(cp, CURRENT) is True
    assert kinds(cp)[-1]["match"] is True


def test_a_run_whose_policy_is_not_the_pinned_one_is_refused(cp):
    cp.set_policy_pin("swarm", cc.sha256_text(TIGHTER), "riley")
    with pytest.raises(Refused, match="approved"):
        cc.check_policy_pin(cp, CURRENT)
    last = kinds(cp)[-1]
    assert last["kind"] == "policy_pin_checked" and last["match"] is False and last["loaded_sha256"] == cc.sha256_text(CURRENT)


# ---- the demonstration the CLI runs: five refusals, then the person's own approval ---------------------------------------
def test_the_governance_demo_refuses_five_ways_then_activates_with_the_persons_approval(cp):
    stored = {}
    out = cc.demo_sequence(cp, TIGHTER, final_approve=approve_as("risk_owner"), store=lambda sha, body: stored.update({sha: body}), current=CURRENT)
    assert [o["kind"] for o in out] == ["denied"] * 5 + ["policy_activated"]
    assert [o["policies"][0] for o in out[:5]] == ["agents-never-change-governance", "only-risk-owner-approves-governance",
                                                  "no-change-with-failed-checks", "approval-bound-to-candidate-hash",
                                                  "governance-is-not-self-amending"]
    assert cp.policy_pin("swarm") == cc.sha256_text(TIGHTER) and list(stored) == [cc.sha256_text(TIGHTER)]


def test_a_person_who_declines_leaves_the_policy_unchanged(cp):
    out = cc.demo_sequence(cp, TIGHTER, final_approve=approve_as("risk_owner", ok=False), store=lambda *a: None, current=CURRENT)
    assert out[-1]["kind"] == "denied" and out[-1]["policies"] == ["person declined"] and cp.policy_pin("swarm") is None


def test_the_demo_records_pass_the_governance_units(cp):
    from tools import check
    cc.demo_sequence(cp, TIGHTER, final_approve=approve_as("risk_owner"), store=lambda *a: None, current=CURRENT)
    recs = kinds(cp)
    assert check.governance_denied(recs)[0] and check.governance_change(recs)[0], (check.governance_denied(recs), check.governance_change(recs))
