"""Offline tests for scenarios/twin/reset.py: the demo baseline, drift detection, the reset and its verify run.
No RSG: a fake link holds the saved scenarios."""

import importlib.util
import sys

import pytest

from adapters.twin import SCENARIOS

spec = importlib.util.spec_from_file_location("reset", SCENARIOS / "reset.py")
reset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset)


class FakeLink:
    def __init__(self, saved=None, free=True, coverage=(0.77, 0.77, 0.77, 0.65)):
        self.saved = saved if saved is not None else {n: reset.wanted(n) for n in reset.TARGETS}
        self.free, self.coverage, self.ops = free, coverage, []

    def run(self, op):
        self.ops.append(op["op"])
        if op["op"] == "slot":
            return {"free": self.free, "status": None if self.free else {"test_id": "rsg-1", "status": "RUNNING"}}
        if op["op"] == "scenario_get":
            return reset.copy.deepcopy(self.saved[op["name"]])
        if op["op"] == "scenario_put":
            self.saved[op["name"]] = op["config"]
            return {"saved": True}
        if op["op"] == "start":
            return {"test_id": "rsg-v"}
        if op["op"] == "step":
            ratio = self.coverage[op["to"] // 60 - 1]
            return {"first": 1.0, "window": {"network": {"coverage_ratio_rsrp_gt_-110": ratio}}}
        if op["op"] == "stop":
            return {"stopped": True}
        raise AssertionError(op)


def run(monkeypatch, link, *args):
    monkeypatch.setattr(reset, "LocalLink", lambda: link)
    monkeypatch.setattr(sys, "argv", ["reset.py", *args])
    reset.main()


def test_the_committed_baseline_is_the_demo():
    for name in reset.TARGETS:
        cfg = reset.wanted(name)
        assert reset.check(cfg) == []
        assert cfg["System"]["duration"] == reset.TARGETS[name]["duration"]


@pytest.mark.parametrize("drift", ["profile_power", "csv_power", "fault_delay", "extra_anomaly"])
def test_check_finds_drift(drift):
    cfg = reset.wanted("team04-track3-live")
    if drift == "profile_power":
        cfg["Cells"]["Cell_Profiles"][0]["cellsConfig"][0]["power"] = 44
    elif drift == "csv_power":
        rows = cfg["Cells"]["Import"]["CSV Import"].strip().splitlines()
        cfg["Cells"]["Import"]["CSV Import"] = "\n".join([rows[0] + ",Power"] + [r + (",43" if "S4" in r else ",40")
                                                                                 for r in rows[1:]])
    elif drift == "fault_delay":
        cfg["Anomalies"]["Power_Failures"][0]["delay"] = 60
    else:
        cfg["Anomalies"]["Radio_Anomalies"] = [{"name": "x"}]
    assert reset.check(cfg)


def test_reset_rewrites_only_a_drifted_scenario(monkeypatch):
    link = FakeLink()
    link.saved["team04-track3-live"]["Cells"]["Cell_Profiles"][0]["cellsConfig"][0]["power"] = 44
    run(monkeypatch, link)
    assert link.ops.count("scenario_put") == 1
    assert link.saved["team04-track3-live"] == reset.wanted("team04-track3-live")


def test_check_writes_nothing_and_fails_on_drift(monkeypatch):
    link = FakeLink()
    link.saved["team04-track3-4cell-powercut"]["Anomalies"]["Power_Failures"] = []
    with pytest.raises(SystemExit, match="drifted: team04-track3-4cell-powercut"):
        run(monkeypatch, link, "--check")
    assert "scenario_put" not in link.ops and "slot" not in link.ops


def test_reset_refuses_while_a_run_is_going(monkeypatch):
    link = FakeLink(free=False)
    link.saved["team04-track3-live"]["System"]["duration"] = 60
    with pytest.raises(SystemExit, match="not resetting"):
        run(monkeypatch, link)
    assert link.ops == ["slot"]


def test_verify_wants_three_good_minutes_then_the_problem(monkeypatch):
    link = FakeLink()
    run(monkeypatch, link, "--verify")
    assert link.ops[-7:] == ["slot", "start", "step", "step", "step", "step", "stop"]
    with pytest.raises(SystemExit, match="did not give the demo's shape"):
        run(monkeypatch, FakeLink(coverage=(0.77, 0.77, 0.77, 0.75)), "--verify")
