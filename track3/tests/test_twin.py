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


# ---- the line to the twin (no RSG: a fake MCP call) -----------------------------------------------
def local_link(replies: dict):
    from adapters.twin_link import LocalLink
    link, calls = LocalLink("https://rsg.invalid/mcp"), []

    def call(tool, **args):
        calls.append((tool, args))
        return replies[tool](**args) if callable(replies.get(tool)) else replies.get(tool, {})
    link.ns["call"] = call
    return link, calls


def test_local_link_runs_the_ops_in_process():
    paused = {"status": "PAUSED", "sim_time": 60, "first_timestamp": 1704067200.0}
    link, calls = local_link({"simulation_start": {"test_id": "T1"}, "simulation_status": paused,
                              "simulation_query": {"csv": "name,tags,n\nUEReports,,40\n"}})
    assert link.run({"op": "start", "scenario": "team04-track3-4cell-powercut"})["test_id"] == "T1"
    assert calls[0] == ("simulation_start", {"scenario": "team04-track3-4cell-powercut", "offline": True, "paced": True})
    r = link.run({"op": "step", "test_id": "T1", "seconds": 60, "to": 60, "first": None})
    assert r["first"] == 1704067200.0 and r["window"]["t"] == [0, 60] and r["window"]["network"]["ue_samples"] == 40
    assert ("simulation_advance", {"test_id": "T1", "run_for": "60s"}) in calls
    link.run({"op": "stop", "test_id": "T1"})
    assert calls[-1] == ("simulation_stop", {"test_id": "T1"})
    assert not any("force" in args for _, args in calls)


def test_local_link_errors_look_like_the_ssm_links():
    link, _ = local_link({})
    for op in ({"op": "nope"}, {"op": "stop"}):
        try:
            link.run(op)
            raise AssertionError("no error")
        except RuntimeError as e:
            assert str(e).startswith(f"twin {op['op']}: "), e


def test_the_slot_check_reads_the_current_run():
    def idle(test_id):  # what the RSG answers with nothing running (3 Nov 2026)
        raise RuntimeError("simulation_status: Error executing tool simulation_status: no simulation is currently "
                           "active — start one with simulation_start, or pass a concrete test_id")
    link, calls = local_link({"simulation_status": idle})
    assert link.run({"op": "slot"}) == {"free": True, "status": None}
    assert calls == [("simulation_status", {"test_id": "current"})]
    for status, free in (("STOPPED at 2026-11-03T02:36:48.806+00:00Z", True), ("PAUSED ADK Simulation ready", False),
                         ("RUNNING", False)):
        link, _ = local_link({"simulation_status": {"test_id": "rsg-30", "status": status}})
        assert link.run({"op": "slot"})["free"] is free, status

    def down(test_id):
        raise RuntimeError("simulation_status: connection refused")
    link, _ = local_link({"simulation_status": down})
    try:  # anything else refuses: no start on a slot we could not read
        link.run({"op": "slot"})
        raise AssertionError("no error")
    except RuntimeError as e:
        assert "connection refused" in str(e)


def test_run8_sends_one_point_per_minute_with_the_run_fields():
    """The 8-minute test on the saved rsg-14 windows: 8 network + 32 tower records, no heartbeat, every record
    carrying the run, the twin's test id, the trace and the config version."""
    import importlib.util
    import time as real_time
    spec = importlib.util.spec_from_file_location("run8", SCENARIOS / "run8.py")
    run8 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run8)
    windows = json.loads((SCENARIOS / "proof-rsg-14.json").read_text())["windows"]
    ops = []

    class FakeLink:
        def run(self, op):
            ops.append(op["op"])
            return {"slot": {"free": True, "status": None}, "start": {"test_id": "rsg-99"}, "events": [], "stop": {},
                    "step": {"first": 1.0, "window": windows[op.get("to", 60) // 60 - 1]}}[op["op"]]
    with tempfile.TemporaryDirectory() as tmp:
        run8.HERE, run8.LocalLink = Path(tmp), FakeLink
        run8.time = SimpleNamespace(monotonic=real_time.monotonic, sleep=lambda s: None)
        argv, sys.argv = sys.argv, ["run8.py", "--link", "local", "--dt", "dry-run", "--config-version", "v-test"]
        twin_dt.HEARTBEAT_S, heartbeat_s = 0.01, twin_dt.HEARTBEAT_S  # a heartbeat, if any, would fire many times
        try:
            run8.main()
            real_time.sleep(0.05)
        finally:
            sys.argv, twin_dt.HEARTBEAT_S = argv, heartbeat_s
        lines = [json.loads(line) for f in Path(tmp).glob("dev-runs/*/dynatrace-dry-run.jsonl")
                 for line in f.read_text().splitlines()]
    assert ops == ["slot", "start"] + ["step", "events"] * 8 + ["stop"], ops
    kinds = [r["twin.kind"] for r in lines]
    assert (kinds.count("network"), kinds.count("cell"), len(lines)) == (8, 32, 40), kinds
    assert not any("twin.heartbeat" in r for r in lines)
    assert [r["twin.minute"] for r in lines if r["twin.kind"] == "network"] == list(range(1, 9))
    for r in lines:
        assert r["service.name"] == "viavi-twin" and r["twin.config_version"] == "v-test" and r["twin.test_id"] == "rsg-99"
        assert r["twin.run_id"].endswith("-twin-8min") and re.fullmatch(r"[0-9a-f]{32}", r["trace_id"])


def test_run8_refuses_a_busy_slot_and_leaves_it_alone():
    import importlib.util
    spec = importlib.util.spec_from_file_location("run8", SCENARIOS / "run8.py")
    run8 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run8)
    ops = []

    class BusyLink:
        def run(self, op):
            ops.append(op["op"])
            return {"free": False, "status": {"test_id": "rsg-50", "status": "PAUSED ADK Simulation ready"}}
    with tempfile.TemporaryDirectory() as tmp:
        run8.HERE, run8.LocalLink = Path(tmp), BusyLink
        argv, sys.argv = sys.argv, ["run8.py", "--link", "local", "--dt", "dry-run"]
        try:
            run8.main()
            raise AssertionError("started on a busy slot")
        except SystemExit as e:
            assert "rsg-50" in str(e.code) and "busy" in str(e.code), e.code
        finally:
            sys.argv = argv
    assert ops == ["slot"], ops  # no start, and no stop of someone else's run


def test_the_token_comes_from_the_environment_first():
    import os
    saved_env, saved_file = os.environ.get("DT_PLATFORM_TOKEN"), twin_dt.ENV_FILE
    with tempfile.TemporaryDirectory() as tmp:
        try:
            twin_dt.ENV_FILE = Path(tmp) / ".env"
            twin_dt.ENV_FILE.write_text("DT_PLATFORM_TOKEN=from-file\n")
            os.environ["DT_PLATFORM_TOKEN"] = "from-env"
            assert twin_dt._token() == "from-env"
            del os.environ["DT_PLATFORM_TOKEN"]
            assert twin_dt._token() == "from-file"  # the laptop's way, unchanged
            twin_dt.ENV_FILE = Path(tmp) / "missing.env"
            try:
                twin_dt._token()
                raise AssertionError("no error")
            except SystemExit as e:
                assert "DT_PLATFORM_TOKEN" in str(e.code)
        finally:
            twin_dt.ENV_FILE = saved_file
            if saved_env is not None:
                os.environ["DT_PLATFORM_TOKEN"] = saved_env


def test_the_ssm_program_still_runs_on_the_host():
    import os
    import subprocess
    from adapters.twin_link import BOX
    out = subprocess.run([sys.executable, "-", json.dumps({"op": "nope"})], input=BOX, capture_output=True, text=True,
                         env=dict(os.environ, VIAVI_RSG_URL="https://rsg.invalid/mcp"), timeout=30).stdout
    assert json.loads(out.strip().splitlines()[-1]) == {"ok": False, "error": "ValueError: unknown op 'nope'"}, out


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
