"""Changing the swarm's policy, the one thing in the system that needs a person.

A candidate policy file goes through: validation against the schema, the safety invariants (the "never" rules must survive),
an impact preview (which decisions would change), then a risk owner's approval of that exact file (its SHA-256) and Cedar's
decision under policies/governance.cedar. Activation moves a pin in the registry. Every swarm run checks its loaded policy
against that pin and refuses to start under any other.
"""

import hashlib
import re
from pathlib import Path

import cedarpy

from contracts import Refused
from policy import NO_PERMIT, PolicySet

HERE = Path(__file__).parent
GOVERNANCE = HERE / "policies" / "governance.cedar"
SCHEMA = HERE / "policies" / "swarm.cedarschema"
# The safety rules every candidate must still contain, as forbid rules with these ids.
REQUIRED_FORBIDS = ("power-range", "tilt-range", "critical-no-power-cut", "critical-no-tilt-up", "critical-power-step-4db",
                    "critical-neighbour-no-harm", "stabilization-15m", "one-autonomous-change-per-run", "tools-approved-only",
                    "viavi-trace-id-in-reason")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def validate_candidate(text: str, schema_text: str | None = None) -> tuple:
    """(ok, errors): the candidate parses and every context key and action it uses is in the schema."""
    try:
        result = cedarpy.validate_policies(text, schema_text if schema_text is not None else SCHEMA.read_text())
    except Exception as e:  # a syntax error: the parser's message is the answer
        return False, [str(e)[:300]]
    return bool(result.validation_passed), [str(e)[:300] for e in result.errors]


def invariants(text: str) -> tuple:
    """(ok, missing): which of the required safety forbids the candidate no longer has."""
    forbids = {pid for pid, effect in re.findall(r'@id\("([^"]+)"\)\s*\n\s*(permit|forbid)', text) if effect == "forbid"}
    missing = [p for p in REQUIRED_FORBIDS if p not in forbids]
    return not missing, missing


def impact_preview(current: str, candidate: str) -> list:
    """The demo scenarios (tools/demo_policies.py, act 1) whose decision differs between the current and the candidate policy."""
    from tools import demo_policies as demo
    old, new = PolicySet(GOVERNANCE, text=current), PolicySet(GOVERNANCE, text=candidate)  # (the path is not read)
    changed = []
    for act, title, _why, pset, principal, action, resource, ctx, attrs, *_ in demo.SCENARIOS:
        if act != 1 or pset != "swarm":
            continue
        before, after = old.evaluate(principal, action, resource, ctx, attrs)[0], new.evaluate(principal, action, resource, ctx, attrs)[0]
        if before != after:
            changed.append({"scenario": title, "before": before, "after": after})
    return changed


def request_activation(cp, candidate: str, requested_by: str, approve, policy: str = "swarm", store=None, current: str | None = None) -> dict:
    """One policy-change request through the checkpoint. Returns the last record: `policy_activated` or `denied`.
    approve(info) -> (approver, role, approved, note, sha256_the_person_saw). store(sha256, text) keeps the approved file."""
    cp.policy_sets.setdefault("governance", PolicySet(GOVERNANCE))
    resource, sha = f'Policy::"{policy}"', sha256_text(candidate)

    def denied(policies: list, who: str = requested_by) -> dict:
        return cp.record("denied", who=who, action="activate_policy", policy=policy, candidate_sha256=sha, policies=policies)

    cp.record("policy_change_requested", who=requested_by, policy=policy, candidate_sha256=sha)
    ctx = {"human_approved": False, "approver_role": "none", "checks_passed": True, "hash_matches": True}
    if policy == "governance":  # the rules about changing rules: no flow can touch them
        d = cp.decide(requested_by, "activate_policy", resource, ctx, policy_set="governance")
        return denied(d["policies"])
    if current is None:
        current = (HERE / "policies" / f"{policy}.cedar").read_text()
    ok_v, errors = validate_candidate(candidate)
    cp.record("policy_validation", candidate_sha256=sha, ok=ok_v, errors=errors)
    ok_i, missing = invariants(candidate)
    cp.record("policy_invariants", candidate_sha256=sha, ok=ok_i, missing=missing)
    changed = impact_preview(current, candidate) if ok_v else []
    cp.record("policy_impact", candidate_sha256=sha, changed=changed)
    ctx["checks_passed"] = ok_v and ok_i

    first = cp.decide(requested_by, "activate_policy", resource, ctx, policy_set="governance")
    if first["decision"] == "Deny" and first["policies"] != [NO_PERMIT]:
        return denied(first["policies"])  # a named rule refuses it: no approval can change that
    hypothetical = {**ctx, "human_approved": True, "approver_role": "risk_owner"}
    if cp.evaluate(requested_by, "activate_policy", resource, hypothetical, policy_set="governance")[0] != "Allow":
        return denied(["not allowed even with approval"])

    cp.record("approval_requested", who=requested_by, policy=policy, candidate_sha256=sha, impact=changed)
    approver, role, approved, note, seen = approve({"candidate_sha256": sha, "impact": changed, "checks": {"validation": ok_v, "invariants": ok_i}})
    cp.record("approval", action="activate_policy", policy=policy, approver=approver, approver_role=role, approved=approved, note=note,
              candidate_sha256=seen)
    if not approved:
        return denied(["person declined"], who=approver)
    final = cp.decide(f'Person::"{approver}"', "activate_policy", resource,
                      {**ctx, "human_approved": True, "approver_role": role, "hash_matches": seen == sha}, policy_set="governance")
    if final["decision"] != "Allow":
        return denied(final["policies"], who=approver)
    (store or (lambda *_: None))(sha, candidate)
    previous = cp.policy_pin(policy)
    cp.set_policy_pin(policy, sha, approver)
    return cp.record("policy_activated", policy=policy, sha256=sha, previous=previous, approver=approver, approver_role=role, impact=changed)


def check_policy_pin(cp, loaded_text: str):
    """At the start of a swarm run: the policy this runtime loaded must be the one a risk owner pinned. Refuses otherwise.
    Returns True (matches), None (nothing pinned yet)."""
    loaded, pinned = sha256_text(loaded_text), cp.policy_pin("swarm")
    match = None if pinned is None else loaded == pinned
    cp.record("policy_pin_checked", loaded_sha256=loaded, pinned_sha256=pinned, match=match,
              note="no policy has been pinned yet" if pinned is None else "")
    if match is False:
        raise Refused("this runtime's policy is not the one a risk owner approved (policy pin mismatch)")
    return match


def demo_sequence(cp, candidate: str, final_approve, store=None, current: str | None = None,
                  requested_by: str = 'Person::"operator"') -> list:
    """The governance demonstration, in one run: five refused ways of changing the rules, then the real thing.
    Refusals 2 and 4 use a scripted approver (a wrong role; a file edited after review) and say so in the record; the final
    approval is final_approve's: a person's own. Returns each attempt's last record."""
    scripted = lambda role, edit="": (lambda info: ("scripted", role, True, "scripted refusal demo" + (": " + edit if edit else ""),
                                                   sha256_text(candidate + "\n// edited after review") if edit else info["candidate_sha256"]))  # noqa: E731
    without_safety = re.sub(r'@id\("power-range"\)\nforbid[^;]*;', "", candidate)
    out = [
        request_activation(cp, candidate, 'Agent::"orchestrator"', lambda info: None, store=store, current=current),    # an agent
        request_activation(cp, candidate, requested_by, scripted("ai_engineer"), store=store, current=current),        # the wrong role
        request_activation(cp, without_safety, requested_by, scripted("risk_owner"), store=store, current=current),     # a safety rule removed
        request_activation(cp, candidate, requested_by, scripted("risk_owner", "the file changed after the review"), store=store, current=current),
        request_activation(cp, "// a rewrite of the governance rules", requested_by, scripted("risk_owner"), policy="governance",
                           store=store, current=""),                                                                 # the rules about the rules
        request_activation(cp, candidate, requested_by, final_approve, store=store, current=current),                  # the person
    ]
    return out
