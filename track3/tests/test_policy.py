from pathlib import Path

import pytest

from policy import PolicySet

POLICIES = Path(__file__).resolve().parent.parent / "policies"
PROMO = PolicySet(POLICIES / "promotion.cedar")
NETWORK = 'Network::"mobile-env-medium"'
LOOP = 'Loop::"improvement"'
CANDIDATE = 'Prompt::"network-agent@v2"'
ALLOWED = {"human_approved": True, "approver_role": "ai_engineer", "eval_passed": True, "open_blocking_issues": 0}


def decision(ps, *args, **kw):
    d, reasons, _errors = ps.evaluate(*args, **kw)
    return d, reasons


def test_promotion_allowed_for_ai_engineer_with_evidence():
    assert decision(PROMO, LOOP, "promote", CANDIDATE, ALLOWED) == ("Allow", ["promote-needs-ai-engineer-and-evidence"])


@pytest.mark.parametrize("principal,resource,change,reasons", [
    ('Agent::"network-agent"', CANDIDATE, {}, ["agents-never-promote"]),
    (LOOP, CANDIDATE, {"approver_role": "network_operator"}, ["operators-cannot-approve-agent-changes"]),
    (LOOP, CANDIDATE, {"eval_passed": False}, ["no-promotion-below-target"]),
    (LOOP, CANDIDATE, {"open_blocking_issues": 2}, ["no-promotion-with-open-signal-issue"]),
    (LOOP, CANDIDATE, {"open_blocking_issues": -1}, ["no policy permits this call"]),
    (LOOP, CANDIDATE, {"human_approved": False}, ["no policy permits this call"]),
    (LOOP, 'Policy::"swarm"', {}, ["loop-never-touches-policy"]),
])
def test_promotion_denials(principal, resource, change, reasons):
    assert decision(PROMO, principal, "promote", resource, {**ALLOWED, **change}) == ("Deny", reasons)


def test_checkpoint_decide_records_policy_set(offline_cp):
    rec = offline_cp.decide(LOOP, "promote", CANDIDATE, ALLOWED, {"sha256": "abc"}, policy_set="promotion")
    assert rec["policy_set"] == "promotion"
    assert rec["decision"] == "Allow"


@pytest.mark.parametrize("name", ["", "network"])
def test_base_checkpoint_has_only_the_promotion_set(offline_cp, name):
    """track3.cedar is gone: the base checkpoint no longer has a default 'network' set to fall back on."""
    assert sorted(offline_cp.policy_sets) == ["promotion"]
    with pytest.raises(KeyError, match="unknown policy set"):
        offline_cp.decide('Agent::"x"', "read_network", NETWORK, {}, policy_set=name)
