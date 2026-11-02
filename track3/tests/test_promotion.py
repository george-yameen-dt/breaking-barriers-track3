import json
from pathlib import Path

import pytest

import incident
import promotion
from promotion import PromptRegistry, blocking_issue_count, experiment_passed, promote
from tools import check

TRACK3 = Path(__file__).resolve().parent.parent
GOOD = {"eval_passed": True, "open_blocking_issues": 0, "experiment_run_id": "exp-1"}


class FakeDynamo:
    """Just the two DynamoDB calls PromptRegistry makes, kept in memory."""

    def __init__(self):
        self.rows = {}

    def put_item(self, TableName, Item):
        self.rows[Item["model_id"]["S"]] = Item

    def get_item(self, TableName, Key, **_):
        item = self.rows.get(Key["model_id"]["S"])
        return {"Item": item} if item else {}


@pytest.fixture
def registry():
    reg = PromptRegistry(FakeDynamo())
    reg.register("network-agent", "v2", "candidate prompt", "tester")
    return reg


def approver(role, ok=True):
    calls = []

    def approve(agent, version, evidence):
        calls.append((agent, version))
        return "tester", role, ok, "test"
    approve.calls = calls
    return approve


def never_asked(*_):
    raise AssertionError("a person must not be asked when the policy already refuses")


def kinds(cp):
    return [r["kind"] for r in check.load(cp.local)]


def test_registry_roundtrip():
    reg = PromptRegistry(FakeDynamo())
    item = reg.register("network-agent", "v1", "one", "tester")
    assert item["status"] == "candidate" and len(item["sha256"]) == 64
    assert reg.get("network-agent", "v1")["text"] == "one"
    assert reg.active("network-agent") is None
    assert reg.activate("network-agent", "v1", "tester") is None
    reg.register("network-agent", "v2", "two", "tester")
    assert reg.activate("network-agent", "v2", "tester") == "v1"
    assert reg.active("network-agent") == "v2"
    assert reg.get("network-agent", "v1")["status"] == "retired"
    assert reg.get("network-agent", "v2")["status"] == "approved"


def test_register_refuses_to_reset_a_live_version():
    reg = PromptRegistry(FakeDynamo())
    reg.register("network-agent", "v1", "one", "tester")
    reg.activate("network-agent", "v1", "tester")
    with pytest.raises(ValueError, match="approved"):
        reg.register("network-agent", "v1", "changed", "tester")


def test_ai_engineer_promotes_candidate(offline_cp, registry):
    rec = promote(offline_cp, registry, "network-agent", "v2", GOOD, approver("ai_engineer"))
    assert rec["kind"] == "promoted" and rec["previous"] is None and rec["approver_role"] == "ai_engineer"
    k = kinds(offline_cp)
    assert k.index("approval_requested") < k.index("approval") < k.index("promoted")
    assert registry.active("network-agent") == "v2"


def test_operator_approval_is_denied(offline_cp, registry):
    rec = promote(offline_cp, registry, "network-agent", "v2", GOOD, approver("network_operator"))
    assert rec["kind"] == "denied"
    assert rec["policies"] == ["operators-cannot-approve-agent-changes"]
    assert registry.active("network-agent") is None


def test_agent_cannot_promote_itself(offline_cp, registry):
    rec = promote(offline_cp, registry, "network-agent", "v2", GOOD, never_asked, requested_by='Agent::"network-agent"')
    assert rec["kind"] == "denied" and rec["policies"] == ["agents-never-promote"]


def test_below_target_denied_without_asking(offline_cp, registry):
    rec = promote(offline_cp, registry, "network-agent", "v2", {**GOOD, "eval_passed": False}, never_asked)
    assert rec["kind"] == "denied" and rec["policies"] == ["no-promotion-below-target"]


def test_arize_unreachable_fails_closed(offline_cp, registry):
    rec = promote(offline_cp, registry, "network-agent", "v2", {**GOOD, "open_blocking_issues": -1}, never_asked)
    assert rec["kind"] == "denied"
    assert registry.active("network-agent") is None


def test_declined_is_recorded(offline_cp, registry):
    rec = promote(offline_cp, registry, "network-agent", "v2", GOOD, approver("ai_engineer", ok=False))
    assert rec["kind"] == "denied" and rec["policies"] == ["person declined"]
    assert registry.active("network-agent") is None


def test_unregistered_candidate_denied(offline_cp, registry):
    rec = promote(offline_cp, registry, "network-agent", "v9", GOOD, never_asked)
    assert rec["kind"] == "denied" and rec["policies"] == ["not a registered candidate"]


def test_blocking_issue_count_fails_closed(monkeypatch):
    monkeypatch.setattr(promotion.arize_signal, "blocking", lambda: [{"id": "a"}, {"id": "b"}])
    assert blocking_issue_count() == 2

    def unreachable():
        raise OSError("no network")
    monkeypatch.setattr(promotion.arize_signal, "blocking", unreachable)
    assert blocking_issue_count() == -1


def test_experiment_passed_reads_the_candidates_result():
    recs = [{"kind": "experiment_result", "agent": "network-agent", "candidate": "v2", "eval_passed": True},
            {"kind": "experiment_result", "agent": "network-agent", "candidate": "v3", "eval_passed": False}]
    assert experiment_passed(recs, "network-agent", "v2") is True
    assert experiment_passed(recs, "network-agent", "v3") is False
    assert experiment_passed(recs, "network-agent", "v4") is False
    assert experiment_passed([], "network-agent", "v2") is False


def test_check_units(offline_cp, registry, tmp_path):
    promote(offline_cp, registry, "network-agent", "v2", GOOD, approver("ai_engineer"))
    assert check.promote(check.load(offline_cp.local))[0] is True
    assert check.promote_denied(check.load(offline_cp.local))[0] is False


def test_check_units_on_a_denial(offline_cp, registry):
    promote(offline_cp, registry, "network-agent", "v2", GOOD, approver("network_operator"))
    recs = check.load(offline_cp.local)
    assert check.promote_denied(recs)[0] is True
    assert check.promote(recs)[0] is False


def test_v1_prompt_is_the_running_prompt():
    assert (TRACK3 / "prompts" / "network-agent" / "v1.txt").read_text() == incident.DIAGNOSE
