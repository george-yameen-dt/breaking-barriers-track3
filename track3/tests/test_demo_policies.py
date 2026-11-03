"""The policy demo (tools/demo_policies.py) is the explanation: every scenario's decision and deciding policy must be what the real policy files say."""

from tools import demo_policies as demo


def test_every_scenario_gets_the_decision_and_policy_it_claims():
    results = demo.run_all()
    assert len(results) >= 28
    wrong = [(r["title"], r["decision"], r["policies"], r["expect_decision"], r["expect_policy"]) for r in results if not r["ok"]]
    assert not wrong, wrong


def test_the_demo_covers_all_three_policy_sets_and_every_forbid_rule_that_matters():
    results = demo.run_all()
    assert {r["act"] for r in results} == {1, 2, 3}
    named = {p for r in results for p in r["policies"]}
    for rule in ("apply-autonomous-within-envelope", "power-range", "critical-power-step", "critical-no-power-cut",
                 "critical-neighbour-no-harm", "stabilization-15m", "one-autonomous-change-per-run", "viavi-trace-id-in-reason",
                 "tools-approved-only", "revert-exact", "agents-never-change-governance", "only-risk-owner-approves-governance",
                 "no-change-with-failed-checks", "approval-bound-to-candidate-hash", "governance-is-not-self-amending",
                 "governance-change-needs-risk-owner-and-evidence", "agents-never-promote", "operators-cannot-approve-agent-changes",
                 "no-promotion-below-target", "no-promotion-with-open-signal-issue", "loop-never-touches-policy"):
        assert rule in named, rule


def test_every_policy_id_in_the_files_is_explained_in_the_document():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    doc = (root / "POLICIES.md").read_text()
    for f in ("swarm", "promotion", "governance"):
        for pid in re.findall(r'@id\("([^"]+)"\)', (root / "policies" / f"{f}.cedar").read_text()):
            assert f"`{pid}`" in doc, f"{pid} ({f}.cedar) is not explained in POLICIES.md"
