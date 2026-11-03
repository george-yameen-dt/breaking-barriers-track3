"""Reprints every reported result from evidence/, with no AWS access.

Exits with an error if the frozen scenario or the policies (swarm or promotion) changed since a run was declared.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from check import UNITS, load  # noqa: E402

ROOT = Path(__file__).parent.parent


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def policy_file(decl: dict) -> Path:
    """The policy text a run was declared with: its archived version by hash, else the path it names."""
    archived = ROOT / "policies" / "versions" / f"{decl.get('policies_sha256', '')}.cedar"
    return archived if archived.exists() else ROOT / decl.get("policies_path", "policies/swarm.cedar")


def main() -> int:
    bad = 0
    results = {}
    for decl_path in sorted((ROOT / "evidence" / "declared").glob("*.json")):
        decl = json.loads(decl_path.read_text())
        run_id = decl["run_id"]
        records = ROOT / "evidence" / "runs" / run_id / "records.jsonl"
        if not records.exists():
            print(f"{run_id}: declared but no records")
            continue
        if sha(ROOT / decl["test_set"]) != decl["test_set_sha256"]:
            print(f"{run_id}: test set changed since it was declared")
            bad += 1
        if decl.get("frozen_state_sha256") and sha(ROOT / "scenarios" / "frozen-state.json") != decl["frozen_state_sha256"]:
            print(f"{run_id}: frozen state changed since it was declared")
            bad += 1
        if decl.get("policies_sha256") and sha(policy_file(decl)) != decl["policies_sha256"]:
            print(f"{run_id}: policies changed since it was declared")
            bad += 1
        if decl.get("promotion_policies_sha256") and sha(ROOT / "policies" / "promotion.cedar") != decl["promotion_policies_sha256"]:
            print(f"{run_id}: promotion policies changed since it was declared")
            bad += 1
        if decl.get("governance_policies_sha256") and sha(ROOT / "policies" / "governance.cedar") != decl["governance_policies_sha256"]:
            print(f"{run_id}: governance policies changed since it was declared")
            bad += 1
        recs = load(records)
        units = [u for u in decl["units"] if u in UNITS] + (["judge"] if "judge" in decl.get("target", {}) else [])
        results[run_id] = {}
        for u in units:
            ok, why = UNITS[u](recs, **decl.get("thresholds", {}).get(u, {}))
            results[run_id][u] = "pass" if ok else "fail"
            print(f"{run_id}  {u:6} {'PASS' if ok else 'FAIL'}  {why}")
    (ROOT / "evidence" / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
