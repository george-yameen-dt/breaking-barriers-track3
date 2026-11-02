from pathlib import Path

import pytest

from policy import PolicySet

POLICIES = Path(__file__).resolve().parent.parent / "policies"
NET = PolicySet(POLICIES / "track3.cedar")
PROMO = PolicySet(POLICIES / "promotion.cedar")
NETWORK = 'Network::"mobile-env-medium"'
LOOP = 'Loop::"improvement"'
CANDIDATE = 'Prompt::"network-agent@v2"'
ALLOWED = {"human_approved": True, "approver_role": "ai_engineer", "eval_passed": True, "open_blocking_issues": 0}


def decision(ps, *args, **kw):
    d, reasons, _errors = ps.evaluate(*args, **kw)
    return d, reasons


def test_network_set_matches_frozen_scenario():
    agent = 'Agent::"change-agent"'
    assert decision(NET, agent, "restart_all_towers", NETWORK, {"human_approved": False, "towers": 0}) == ("Deny", ["no-network-wide-restart"])
    assert decision(NET, agent, "rollback_model", NETWORK, {"human_approved": False, "towers": 0}) == ("Deny", ["no policy permits this call"])
    assert decision(NET, agent, "rollback_model", NETWORK, {"human_approved": True, "towers": 0}) == ("Allow", ["rollback-needs-human"])
    assert decision(NET, agent, "invoke_model", 'Model::"m"', {}, {"status": "withdrawn"})[0] == "Deny"


def test_promotion_allowed_for_ai_engineer_with_evidence():
    assert decision(PROMO, LOOP, "promote", CANDIDATE, ALLOWED) == ("Allow", ["promote-needs-ai-engineer-and-evidence"])


@pytest.mark.parametrize("principal,resource,change,reasons", [
    ('Agent::"network-agent"', CANDIDATE, {}, ["agents-never-promote"]),
    (LOOP, CANDIDATE, {"approver_role": "network_operator"}, ["operators-cannot-approve-agent-changes"]),
    (LOOP, CANDIDATE, {"eval_passed": False}, ["no-promotion-below-target"]),
    (LOOP, CANDIDATE, {"open_blocking_issues": 2}, ["no-promotion-with-open-signal-issue"]),
    (LOOP, CANDIDATE, {"open_blocking_issues": -1}, ["no policy permits this call"]),
    (LOOP, CANDIDATE, {"human_approved": False}, ["no policy permits this call"]),
    (LOOP, 'Policy::"track3.cedar"', {}, ["loop-never-touches-policy"]),
])
def test_promotion_denials(principal, resource, change, reasons):
    assert decision(PROMO, principal, "promote", resource, {**ALLOWED, **change}) == ("Deny", reasons)


def test_checkpoint_decide_records_policy_set(offline_cp):
    rec = offline_cp.decide(LOOP, "promote", CANDIDATE, ALLOWED, {"sha256": "abc"}, policy_set="promotion")
    assert rec["policy_set"] == "promotion"
    assert rec["decision"] == "Allow"
    net = offline_cp.decide('Agent::"network-agent"', "read_network", NETWORK, {})
    assert "policy_set" not in net
    assert net["decision"] == "Allow"
