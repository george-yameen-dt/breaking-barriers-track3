"""Offline tests for the Viavi twin adapter (adapters/twin.py) and the swarm's hands (apply, verify, undo).
No AWS, no SSM, no model calls: the twin is Stan's saved run (scenarios/twin/proof-rsg-14.json).

Run:  cd track3 && ~/.local/venvs/mobile-env/bin/python tests/test_twin.py
"""

import copy
import json
import re
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import risk_checks as rc  # noqa: E402
from adapters import twin_dt  # noqa: E402
from adapters.twin import SCENARIOS, RecordedTwin, TwinNetwork, open_twin, uncovered_fraction  # noqa: E402
from adapters.twin import snapshot as twin_snapshot  # noqa: E402
from agents.governance import POLICIES, cedar_decide  # noqa: E402
from agents.orchestrator import alert_text, apply_and_verify, apply_context  # noqa: E402

SCENARIO = "s3-power-failure"
SPEC = json.loads((SCENARIOS / f"{SCENARIO}.json").read_text())["twin"]


def twin_net(twin_cls=RecordedTwin) -> TwinNetwork:
    return TwinNetwork(twin_cls(SCENARIOS / SPEC["recording"], SPEC["recording_first_timestamp"]), SCENARIO, "test", "t")


class FakeCheckpoint:
    """Records in a list, and real Cedar decisions on policies/swarm.cedar."""
    run_id, trace_id = "test-run", "0" * 32

    def __init__(self):
        self.records = []
        self.policies = POLICIES.read_text()
        self.ids = re.findall(r'@id\("([^"]+)"\)', self.policies)

    def record(self, kind: str, **fields) -> dict:
        self.records.append({"kind": kind, **fields})
        return self.records[-1]

    def decide(self, principal, action, resource, context) -> dict:
        decision, policies, _ = cedar_decide(self.policies, self.ids, principal, action, resource, context)
        return {"decision": decision, "policies": policies}


def chosen(net, cell_id="S4", parameter="tx_power_dbm", value=44) -> tuple:
    p = {"proposal_id": "PROP-coverage-1", "agent": "coverage", "cell_id": cell_id, "parameter": parameter,
         "current_value": net.config[cell_id][parameter], "proposed_value": value}
    return p, apply_context(net, cell_id, parameter, value, approval_valid=True)


# ---- the snapshot ---------------------------------------------------------------------------------
def test_snapshot_from_the_saved_incident_window():
    net = twin_net()
    n = net.snapshot["network"]
    assert (n["phones_active"], n["phones_dropped"]) == (39.0, 16.0), n
    cells = {c["cell_id"]: c for c in net.snapshot["cells"]}
    assert cells["S3"]["enabled"] == 0 and cells["S3"]["active_users"] == 0.0
    assert cells["S4"]["tx_power_dbm"] == 40, "the saved run's own S4 change at 360 s is not in effect yet"
    assert (cells["S1"]["tx_power_dbm"], cells["S1"]["tilt_deg"]) == (44, 4), "the saved run's 180 s change is"
    assert cells["S4"]["dropped_nearby"] > cells["S2"]["dropped_nearby"] > cells["S1"]["dropped_nearby"]


def test_snapshot_hash_is_deterministic():
    assert twin_net().snapshot["snapshot_sha256"] == twin_net().snapshot["snapshot_sha256"]


def test_alarms_come_from_the_twins_events():
    net = twin_net()
    s3 = net.alarm_history("S3")
    assert s3 and all(a["kind"] == "alarm" for a in s3) and any(a["hardware_fault"] for a in s3), s3
    assert any("radio link" in a["text"] for a in net.alarm_history("S4"))
    text = alert_text(net.snapshot, net.alarms)
    assert "16.0 of 39.0" in text and "S4" in text and "S3" in text, text


def test_earlier_changes_are_the_operators_not_an_agents():
    net = twin_net()
    assert {e["by"] for e in net.ledger} == {"operator"}
    assert rc.minutes_since_agent_change(net.ledger, "S1") == 9999


def test_uncovered_fraction_moves_with_power():
    base = uncovered_fraction(-106.24, -134.0)
    assert 0 < uncovered_fraction(-106.24, -134.0, 4.0) < base < uncovered_fraction(-106.24, -134.0, -4.0) < 1


# ---- the what-if ----------------------------------------------------------------------------------
def test_power_up_on_s4_is_predicted_to_help():
    net = twin_net()
    sim = net.simulate("S4", "tx_power_dbm", 44)
    assert sim["deltas"]["network.phones_dropped"] < 0, sim["deltas"]
    assert sim["simulation_ref"] == net.simulate("s4", "tx_power_dbm", "44")["simulation_ref"]
    assert sim["deltas"]["S2.sinr_db_p10"] is None, "neighbours are measured, not predicted"


def test_the_whatif_refuses_what_the_twin_cannot_do():
    net = twin_net()
    for args, words in ((("S3", "enabled", 1), "cannot switch"), (("S3", "tx_power_dbm", 44), "not transmitting"),
                        (("S1", "tx_power_dbm", 50), "safe range"), (("S4", "tx_power_dbm", 33), "safe range"),
                        (("S4", "tilt_deg", 3), "safe range"), (("S9", "tilt_deg", 5), "unknown cell")):
        assert words in net.simulate(*args).get("error", ""), args


# ---- the checks and Cedar -------------------------------------------------------------------------
def test_risk_checks_use_the_twins_whatif():
    net = twin_net()
    p, _ = chosen(net)
    p["simulation_ref"], p["confidence"] = net.simulate("S4", "tx_power_dbm", 44)["simulation_ref"], 0.8
    c = rc.check(p, {"S4.dropped_nearby": 8.7}, net.snapshot, net.layout, net.config, net.ledger, [p], {},
                 simulate=net.simulate)
    assert (c["risk"], c["recommendation"]) == ("low", "approval_required"), c["failed"]
    bad = dict(p, cell_id="S3", parameter="enabled", proposed_value=1, current_value=1)
    c = rc.check(bad, {}, net.snapshot, net.layout, net.config, net.ledger, [bad], {}, simulate=net.simulate)
    assert c["recommendation"] == "reject" and "cannot switch" in c["reasons"][0], c


def test_cedar_denies_the_twins_deny_probe():
    net, cp = twin_net(), FakeCheckpoint()
    probe = net.fault["deny_probe"]
    ctx = apply_context(net, probe["cell_id"], probe["parameter"], probe["value"], approval_valid=False)
    d = cp.decide('Agent::"orchestrator"', "apply_cell_configuration", f'Cell::"{probe["cell_id"]}"', ctx)
    assert d["decision"] == "Deny" and "power-range" in d["policies"], d


# ---- the hands ------------------------------------------------------------------------------------
def test_hands_keep_a_change_that_helps():
    net, cp = twin_net(), FakeCheckpoint()
    run = SimpleNamespace(cp=cp, net=net)
    p, ctx = chosen(net)
    outcome = apply_and_verify(run, p, ctx, "APPROVAL-test-1", "16.0 -> 14.6", say=lambda s: None)
    assert outcome.startswith("applied and verified"), outcome
    kinds = [r["kind"] for r in cp.records]
    assert kinds == ["applied", "kpi_snapshot", "verification"], kinds
    v = cp.records[-1]
    assert (v["status"], v["phones_without_service_before"], v["measured_after"]) == ("VERIFIED", 16.0, 12.0), v
    assert net.config["S4"]["tx_power_dbm"] == 44 and net.ledger[-1]["by"] == "agent"
    assert "trace_id=" in net.twin.sent[0]["reason"] and net.twin.sent[0]["params"] == {"cell": "S4/B3/C1", "power": 44}


class WorseTwin(RecordedTwin):
    """The saved run, except that every window after the change is worse."""

    def window(self, t0, t1):
        w = super().window(t0, t1)
        if t0 >= 360:
            w = copy.deepcopy(w)
            w["network"]["coverage_ratio_rsrp_gt_-110"] = 0.5
        return w


def test_hands_undo_a_change_that_made_things_worse():
    net, cp = twin_net(WorseTwin), FakeCheckpoint()
    run = SimpleNamespace(cp=cp, net=net)
    p, ctx = chosen(net)
    outcome = apply_and_verify(run, p, ctx, "APPROVAL-test-1", "16.0 -> 14.6", say=lambda s: None)
    assert "undone" in outcome, outcome
    v = next(r for r in cp.records if r["kind"] == "verification")
    assert v["status"] == "VERIFY_FAILED" and v["measured_after"] > v["phones_without_service_before"], v
    undo = next(r for r in cp.records if r["kind"] == "reverted")
    assert undo["policies"] == ["revert-exact"] and undo["restored_value"] == 40, undo
    assert net.config["S4"]["tx_power_dbm"] == 40
    assert [s["params"]["power"] for s in net.twin.sent] == [44, 40]


def test_open_twin_recorded():
    net = open_twin("recorded", SCENARIO, "test", "t")
    assert net.source == "viavi-twin (recorded)" and net.fault["deny_probe"]["value"] == 50


# ---- the Dynatrace problem card (prepared; nothing is sent) -----------------------------------------
DETECTOR = json.loads((SCENARIOS / "dynatrace" / "detector.json").read_text())


def test_dynatrace_records_from_the_incident_snapshot():
    net = twin_net()
    records = twin_dt.snapshot_records(net.snapshot, "run-1", "a" * 32, "recorded")
    network = records[0]
    assert (network["twin.kind"], network["service.name"]) == ("network", "viavi-twin")
    assert (network["twin.phones_without_service"], network["twin.worst_cell"], network["twin.dark_cells"]) == (16.0, "S4", "S3")
    assert [r["twin.cell"] for r in records[1:]] == ["S1", "S2", "S3", "S4"]
    alerts = {r.get("twin.cell", "network"): r["twin.alert"] for r in records if "twin.alert" in r}
    assert alerts == {"S3": "tower_down"}, alerts  # the dark tower carries the problem, not the network
    net.apply("S4", "tx_power_dbm", 44, reason="test")
    after = twin_dt.snapshot_records(net.measure(), "run-1", "a" * 32, "recorded")
    assert not [r for r in after if "twin.alert" in r], "12 phones without service is under the alert line"


def test_the_pipeline_reads_the_fields_the_sender_writes():
    pipeline = json.loads((SCENARIOS / "dynatrace" / "pipeline.json").read_text())
    records = twin_dt.snapshot_records(twin_net().snapshot, "run-1", "a" * 32, "recorded")
    tower = next(r for r in records if r.get("twin.alert") == "tower_down")
    written = set().union(*records)
    for p in pipeline["davis"]["processors"] + pipeline["smartscapeNodeExtraction"]["processors"]:
        used = set(re.findall(r"\{(twin\.[a-z_]+)\}", json.dumps(p))) | set(re.findall(r'"(twin\.[a-z_]+)"', json.dumps(p)))
        assert used <= written, (p["id"], used - written)
    tower_event = next(p for p in pipeline["davis"]["processors"] if p["id"] == "gy_twin_tower_down")
    assert tower["twin.alert"] in tower_event["matcher"]


def test_dry_run_writes_records_and_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        dt = twin_dt.DynatraceLogs("dry-run", "run-1", "a" * 32, dry_file=Path(tmp) / "dt.jsonl")
        net = TwinNetwork(RecordedTwin(SCENARIOS / SPEC["recording"], SPEC["recording_first_timestamp"]), SCENARIO,
                          "run-1", "t", dt=dt)
        net.apply("S4", "tx_power_dbm", 44, reason="test")
        net.measure()
        net.close()
        lines = [json.loads(line) for line in (Path(tmp) / "dt.jsonl").read_text().splitlines()]
    # 5 windows before the incident (dated back) + the incident's 5 + 1 change + 5
    assert dt.token == "" and dt.sent == len(lines) == 36, (dt.sent, len(lines))
    assert [r["twin.phones_without_service"] for r in lines if r["twin.kind"] == "network"][-2:] == [16.0, 12.0]
    stamps = [r["timestamp"] for r in lines if r["twin.kind"] == "network"]
    assert stamps[:6] == sorted(stamps[:6]) and stamps[0] < stamps[5], stamps  # history first, oldest first
    change = next(r for r in lines if r["twin.kind"] == "change")
    assert (change["twin.cell"], change["twin.new_value"], change["twin.accepted"]) == ("S4", 44, 1)


def test_detector_fires_only_on_the_outage_window():
    query = DETECTOR["analyzer"]["input"][0]["value"]
    assert 'service.name == "viavi-twin"' in query and DETECTOR["analyzer"]["name"].endswith("RecordAnomalyDetectionAnalyzer")
    threshold = float(re.search(r"phones > (\d+(?:\.\d+)?)", query).group(1))
    data = json.loads((SCENARIOS / SPEC["recording"]).read_text())
    cfg = {c: {"enabled": 1, "tx_power_dbm": 40, "tilt_deg": 6, "classification": "production", "maintenance_window": 0}
           for c in ("S1", "S2", "S3", "S4")}
    firing = [tuple(w["t"]) for w in data["windows"]
              if twin_snapshot(w, cfg, 39.0)["network"]["phones_dropped"] > threshold]
    assert firing == [(300, 360)], firing


# ---- runner ---------------------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [(name, fn) for name, fn in list(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:  # report every failure, keep going
            failed += 1
            where = traceback.extract_tb(e.__traceback__)[-1]
            print(f"FAIL  {name}  ({Path(where.filename).name}:{where.lineno})\n      {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed, {len(tests)} total")
    sys.exit(1 if failed else 0)
