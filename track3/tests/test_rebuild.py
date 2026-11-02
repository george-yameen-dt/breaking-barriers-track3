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
    old = json.loads(next((root / "evidence" / "declared").glob("*incident*.json")).read_text())
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
