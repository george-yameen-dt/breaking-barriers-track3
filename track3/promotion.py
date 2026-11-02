"""Changes to the agents themselves go through the same checkpoint as changes to the network.

A candidate (here: a new version of an agent's prompt) goes live only when the promotion policies
(policies/promotion.cedar) allow it: an AI engineer approves, the experiment met the target declared before
it ran, and Arize Signal has no open critical or high issue. The order of steps mirrors Checkpoint.tool():
ask Cedar first, ask a person only when an approval could make the call allowed, decide again, then act.
"""

import hashlib
from datetime import datetime, timezone

from policy import NO_PERMIT
from tools import arize_signal

REGISTRY_TABLE = "bb26-team04-track3-registry"
PROMOTER = 'Loop::"improvement"'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PromptRegistry:
    """Prompt versions in the model registry table: one item per version, one pointer per agent to the live version."""

    def __init__(self, ddb, table: str = REGISTRY_TABLE):
        self.ddb, self.table = ddb, table

    def _get(self, key: str) -> dict | None:
        item = self.ddb.get_item(TableName=self.table, Key={"model_id": {"S": key}}, ConsistentRead=True).get("Item")
        return {k: next(iter(v.values())) for k, v in item.items()} if item else None

    def _put(self, key: str, who: str, **fields) -> None:
        self.ddb.put_item(TableName=self.table, Item={"model_id": {"S": key}, "changed_by": {"S": who}, "changed_at": {"S": _now()},
                                                      **{k: {"S": str(v)} for k, v in fields.items()}})

    def register(self, agent: str, version: str, text: str, who: str) -> dict:
        existing = self.get(agent, version)
        if existing and existing["status"] != "candidate":
            raise ValueError(f"{agent}@{version} is already {existing['status']}; register a new version instead")
        self._put(f"prompt/{agent}@{version}", who, agent=agent, version=version, status="candidate",
                  sha256=hashlib.sha256(text.encode()).hexdigest(), text=text)
        return self.get(agent, version)

    def get(self, agent: str, version: str) -> dict | None:
        item = self._get(f"prompt/{agent}@{version}")
        return {k: item[k] for k in ("agent", "version", "status", "sha256", "text")} if item else None

    def active(self, agent: str) -> str | None:
        pointer = self._get(f"prompt/{agent}")
        return pointer["active"] if pointer else None

    def activate(self, agent: str, version: str, who: str) -> str | None:
        previous = self.active(agent)
        if previous and previous != version:
            old = self.get(agent, previous)
            self._put(f"prompt/{agent}@{previous}", who, **{**old, "status": "retired"})
        new = self.get(agent, version)
        self._put(f"prompt/{agent}@{version}", who, **{**new, "status": "approved"})
        self._put(f"prompt/{agent}", who, status="pointer", active=version)
        return previous


def blocking_issue_count() -> int:
    """Open critical or high Arize Signal issues; -1 when Arize cannot be read, which the policy treats as a refusal."""
    try:
        return len(arize_signal.blocking())
    except Exception:
        return -1


def experiment_passed(records: list, agent: str, version: str) -> bool:
    """Did this candidate's experiment meet the target declared before it ran? No result means no."""
    results = [r for r in records if r.get("kind") == "experiment_result" and r.get("agent") == agent and r.get("candidate") == version]
    return bool(results) and results[-1].get("eval_passed") is True


def promote(cp, registry, agent: str, version: str, evidence: dict, approve, requested_by: str = PROMOTER) -> dict:
    """Promote one prompt version through the checkpoint. Returns the last record: `promoted` or `denied`."""
    def denied(policies: list) -> dict:
        return cp.record("denied", who=requested_by, action="promote", agent=agent, version=version, policies=policies)

    item = registry.get(agent, version)
    if not item or item["status"] != "candidate":
        return denied(["not a registered candidate"])
    resource, attrs = f'Prompt::"{agent}@{version}"', {"sha256": item["sha256"]}
    context = {"human_approved": False, "approver_role": "none", "eval_passed": bool(evidence["eval_passed"]),
               "open_blocking_issues": int(evidence["open_blocking_issues"])}

    first = cp.decide(requested_by, "promote", resource, context, attrs, policy_set="promotion")
    if first["decision"] == "Deny" and first["policies"] != [NO_PERMIT]:
        return denied(first["policies"])  # a named rule refuses it; no approval can change that
    hypothetical = {**context, "human_approved": True, "approver_role": "ai_engineer"}
    if cp.evaluate(requested_by, "promote", resource, hypothetical, attrs, policy_set="promotion")[0] != "Allow":
        return denied(["not allowed even with approval"])

    cp.record("approval_requested", who=requested_by, action="promote", agent=agent, version=version, evidence=evidence)
    approver, role, ok, note = approve(agent, version, evidence)
    cp.record("approval", action="promote", agent=agent, version=version, approver=approver, approver_role=role, approved=ok, note=note)
    if not ok:
        return denied(["person declined"])

    final = cp.decide(requested_by, "promote", resource, {**context, "human_approved": True, "approver_role": role}, attrs,
                      policy_set="promotion")
    if final["decision"] != "Allow":
        return denied(final["policies"])
    previous = registry.activate(agent, version, approver)
    return cp.record("promoted", agent=agent, version=version, previous=previous, sha256=item["sha256"],
                     approver=approver, approver_role=role, evidence=evidence)
