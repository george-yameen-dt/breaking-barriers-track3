import json
import shutil
from pathlib import Path

import pytest

from tools import rebuild

TRACK3 = Path(__file__).resolve().parent.parent


@pytest.fixture
def copy(tmp_path, monkeypatch):
    """A copy of the evidence, scenarios and policies, so rebuild never writes into the repository."""
    for d in ("evidence", "scenarios", "policies"):
        shutil.copytree(TRACK3 / d, tmp_path / d)
    monkeypatch.setattr(rebuild, "ROOT", tmp_path)
    return tmp_path


def test_rebuild_keeps_old_results(copy):
    assert rebuild.main() == 0
    committed = json.loads((TRACK3 / "evidence" / "results.json").read_text())
    rebuilt = json.loads((copy / "evidence" / "results.json").read_text())
    assert {k: rebuilt[k] for k in committed} == committed


def _declare_promotion(root: Path, promotion_sha: str) -> None:
    old = json.loads(next((root / "evidence" / "declared").glob("*swarm*.json")).read_text())
    decl = {**old, "run_id": "2026-11-02T09-00-track3-promote-01", "units": ["promote_denied"], "target": {"promote_denied": "x"},
            "promotion_policies_sha256": promotion_sha}
    (root / "evidence" / "declared" / f"{decl['run_id']}.json").write_text(json.dumps(decl))
    run = root / "evidence" / "runs" / decl["run_id"]
    run.mkdir(parents=True)
    rec = {"run_id": decl["run_id"], "trace_id": "t", "seq": 1, "time": "x", "kind": "decision", "action": "promote",
           "decision": "Deny", "policies": ["operators-cannot-approve-agent-changes"]}
    (run / "records.jsonl").write_text(json.dumps(rec) + "\n")


def test_rebuild_scores_a_promotion_run(copy):
    _declare_promotion(copy, rebuild.sha(copy / "policies" / "promotion.cedar"))
    assert rebuild.main() == 0
    rebuilt = json.loads((copy / "evidence" / "results.json").read_text())
    assert rebuilt["2026-11-02T09-00-track3-promote-01"] == {"promote_denied": "pass"}


def test_rebuild_fails_when_promotion_policies_changed(copy, capsys):
    _declare_promotion(copy, "0" * 64)
    assert rebuild.main() == 1
    assert "promotion policies changed" in capsys.readouterr().out


def test_policy_file_prefers_version_by_hash(copy):
    decl = {"policies_path": "policies/swarm.cedar", "policies_sha256": "af0caba68fe6f6b8003202982e2da7a59722fedb6ff8842effe51959ada9bdfd"}
    assert rebuild.policy_file(decl) == copy / "policies" / "versions" / f"{decl['policies_sha256']}.cedar"


def test_policy_file_falls_back_to_path(copy):
    decl = {"policies_path": "policies/swarm.cedar", "policies_sha256": "0" * 64}
    assert rebuild.policy_file(decl) == copy / "policies" / "swarm.cedar"


def _declare_autonomous(root: Path, run_id: str, records: list, thresholds: dict) -> None:
    old = json.loads(next((root / "evidence" / "declared").glob("*swarm*.json")).read_text())
    decl = {**old, "run_id": run_id, "units": ["problem_to_command", "detect", "one_problem"],
            "target": {"problem_to_command": "x", "detect": "x", "one_problem": "x"}, "thresholds": thresholds}
    decl.pop("policies_sha256", None)
    (root / "evidence" / "declared" / f"{run_id}.json").write_text(json.dumps(decl))
    run = root / "evidence" / "runs" / run_id
    run.mkdir(parents=True)
    (run / "records.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _autonomous_records(trace: str, command_trace: str, gap_s: int) -> list:
    return [
        {"seq": 1, "kind": "run_start", "problem_id": "P-2611-1", "trace_id": trace},
        {"seq": 3, "kind": "applied", "cell_id": "S4", "parameter": "tx_power_dbm", "old_value": 40, "new_value": 44,
         "reply": {"accepted": True}, "command_reason": f"trace_id={command_trace} run_id=r"},
        {"seq": 9, "kind": "dynatrace_evidence", "fault_start": "2026-11-03T10:00:00Z", "problem_count": 1,
         "problems": [{"display_id": "P-1", "event.start": f"2026-11-03T10:{gap_s // 60:02d}:{gap_s % 60:02d}Z"}]}]


def test_rebuild_follows_trace_from_problem_to_viavi_command(copy, capsys):
    import hashlib
    trace = hashlib.sha256(b"P-2611-1").hexdigest()[:32]
    _declare_autonomous(copy, "2026-11-03T10-00-track3-swarm-01", _autonomous_records(trace, trace, 90), {"detect": {"max_seconds": 300}})
    assert rebuild.main() == 0
    res = json.loads((copy / "evidence" / "results.json").read_text())["2026-11-03T10-00-track3-swarm-01"]
    assert res == {"problem_to_command": "pass", "detect": "pass", "one_problem": "pass"}, capsys.readouterr().out


def test_rebuild_fails_a_late_detection_against_the_declared_threshold(copy):
    import hashlib
    trace = hashlib.sha256(b"P-2611-1").hexdigest()[:32]
    _declare_autonomous(copy, "2026-11-03T10-00-track3-swarm-02", _autonomous_records(trace, "f" * 32, 500), {"detect": {"max_seconds": 300}})
    rebuild.main()
    res = json.loads((copy / "evidence" / "results.json").read_text())["2026-11-03T10-00-track3-swarm-02"]
    assert res["detect"] == "fail" and res["problem_to_command"] == "fail" and res["one_problem"] == "pass"
