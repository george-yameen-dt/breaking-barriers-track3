"""Unit tests for the Track 3 swarm. Offline: no AWS, no dtctl, no model calls, no Checkpoint.

Run:  cd track3 && ~/.local/venvs/mobile-env/bin/python tests/test_swarm.py
(pytest-style test_* functions with plain asserts, so `pytest tests/test_swarm.py` works too.)
"""

import copy
import json
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contracts  # noqa: E402
import dt_tools  # noqa: E402
import rfmodel as m  # noqa: E402
import risk_checks as rc  # noqa: E402
from adapters.replay import ReplayNetwork  # noqa: E402
from agents.governance import cedar_decide  # noqa: E402  (pure function; never build a Checkpoint here)

TRACK3 = Path(__file__).resolve().parent.parent
FAULTS = TRACK3 / "scenarios" / "faults"
POLICY_TEXT = (TRACK3 / "policies" / "swarm.cedar").read_text()
POLICY_IDS = re.findall(r'@id\("([^"]+)"\)', POLICY_TEXT)
NETWORK = 'Network::"mobile-env-medium"'
NO_PERMIT = ["no policy permits this call"]
LAYOUT = m.load_layout()


# ---- helpers -------------------------------------------------------------------------------------
def fault(name: str) -> dict:
    return json.loads((FAULTS / f"{name}.json").read_text())


def fault_config(name: str) -> dict:
    inj = fault(name)["injection"]
    return m.with_change(m.default_config(), inj["cell_id"], inj["parameter"], inj["value"])


def outage_world():
    """outage-C: config, the snapshot the agents were shown, and the fault's own ledger."""
    cfg = fault_config("outage-C")
    snap = m.snapshot(LAYOUT, cfg, snapshot_id="SNAP-test-1", config_version=1, taken_at="2026-11-02T12:00:00Z")
    return cfg, snap, fault("outage-C")["ledger"]


def proposal(cfg, cell_id, parameter, value, pid="PROP-test-1", agent="availability", confidence=0.9, ref=None):
    return {"proposal_id": pid, "agent": agent, "cell_id": cell_id, "parameter": parameter,
            "current_value": int(cfg[cell_id][parameter]), "proposed_value": value,
            "simulation_ref": m.simulation_ref(cfg, cell_id, parameter, value) if ref is None else ref,
            "confidence": confidence}


def run_check(p, evidence=None, ledger=None, proposals=None, reports=None):
    cfg, snap, fault_ledger = outage_world()
    return rc.check(p, evidence or {}, snap, LAYOUT, cfg, fault_ledger if ledger is None else ledger,
                    proposals or [p], reports or {})


def decide(principal, action, resource, context=None, attrs=None):
    return cedar_decide(POLICY_TEXT, POLICY_IDS, principal, action, resource, context or {}, attrs)


def agent(name: str) -> str:
    return f'Agent::"{name}"'


def apply_ctx(parameter="tx_power_dbm", old=43, new=44, minutes=9999) -> dict:
    return {"proposal_id": "PROP-coverage-1", "parameter": parameter, "old_value": old, "new_value": new,
            "delta": new - old, "abs_delta": abs(new - old), "risk_checks_passed": True, "selected_proposal_matches": True,
            "applies_this_run": 0, "minutes_since_last_change": minutes}


def apply(cell="C", principal="orchestrator", **ctx):
    return decide(agent(principal), "apply_cell_configuration", f'Cell::"{cell}"', apply_ctx(**ctx))


# ---- 1. rfmodel ----------------------------------------------------------------------------------
def test_snapshot_hash_is_deterministic():
    cfg = fault_config("outage-C")
    s1 = m.snapshot(LAYOUT, cfg, snapshot_id="SNAP-x-1", config_version=1, taken_at="2026-11-02T12:00:00Z")
    s2 = m.snapshot(LAYOUT, copy.deepcopy(cfg), snapshot_id="SNAP-x-1", config_version=1, taken_at="2026-11-02T12:00:00Z")
    assert s1["snapshot_sha256"] == s2["snapshot_sha256"]
    assert s1["snapshot_sha256"].startswith("sha256:") and len(s1["snapshot_sha256"]) == 7 + 64
    body = {k: v for k, v in s1.items() if k != "snapshot_sha256"}
    assert contracts.sha256_of(body) == s1["snapshot_sha256"], "hash must be recomputable from the snapshot body"
    s3 = m.snapshot(LAYOUT, cfg, snapshot_id="SNAP-x-1", config_version=1, taken_at="2026-11-02T12:01:00Z")
    assert s3["snapshot_sha256"] != s1["snapshot_sha256"], "taken_at is part of what the hash binds"


def test_outage_c_drops_more_phones_than_default():
    base = m.network_kpis(LAYOUT, m.default_config())["network"]["phones_dropped"]
    out = m.network_kpis(LAYOUT, fault_config("outage-C"))["network"]["phones_dropped"]
    assert out > base, (base, out)


def test_switching_c_back_on_restores_baseline():
    base = m.network_kpis(LAYOUT, m.default_config())["network"]["phones_dropped"]
    sim = m.simulate_change(LAYOUT, fault_config("outage-C"), "C", "enabled", 1)
    assert "error" not in sim, sim
    assert sim["current_value"] == 0 and sim["proposed_value"] == 1
    assert sim["predicted_network"]["phones_dropped"] == base, (sim["predicted_network"], base)
    assert sim["deltas"]["network.phones_dropped"] < 0


def test_simulation_ref_is_stable():
    cfg = fault_config("outage-C")
    r1 = m.simulation_ref(cfg, "C", "enabled", 1)
    assert r1 == m.simulation_ref(copy.deepcopy(cfg), "C", "enabled", 1)
    assert r1 == m.simulate_change(LAYOUT, cfg, "C", "enabled", 1)["simulation_ref"]
    assert re.fullmatch(r"SIM-[0-9a-f]{16}", r1), r1
    assert r1 != m.simulation_ref(cfg, "C", "enabled", 0), "a different value is a different what-if"
    assert r1 != m.simulation_ref(m.default_config(), "C", "enabled", 1), "a different network is a different what-if"


def test_simulate_change_rejects_unknown_inputs():
    cfg = m.default_config()
    assert "error" in m.simulate_change(LAYOUT, cfg, "Z", "enabled", 1)
    assert "error" in m.simulate_change(LAYOUT, cfg, "C", "frequency", 1)
    assert "error" in m.simulate_change(LAYOUT, cfg, "C", "tx_power_dbm", "loud")


# ---- 2. risk_checks under outage-C -------------------------------------------------------------
def test_restore_c_is_approval_required_low_risk():
    cfg, _, _ = outage_world()
    p = proposal(cfg, "C", "enabled", 1)
    r = run_check(p, evidence={"C.enabled": 0})
    assert r["checks"]["simulation_ref_valid"] and r["checks"]["evidence_matches_snapshot"], r["checks"]
    assert r["recommendation"] == "approval_required", \
        f"got {r['recommendation']}/{r['risk']}, failed={r['failed']}, deltas={r['predicted']['deltas']}"
    assert r["risk"] == "low", (r["risk"], r["failed"])


def test_power_50_is_out_of_range_and_not_approvable():
    assert rc.in_range("tx_power_dbm", 46) and rc.in_range("tx_power_dbm", 30)
    assert not rc.in_range("tx_power_dbm", 47) and not rc.in_range("tx_power_dbm", 29)
    cfg, _, _ = outage_world()
    r = run_check(proposal(cfg, "B", "tx_power_dbm", 50, agent="coverage"))
    assert r["checks"]["in_approved_range"] is False
    assert r["recommendation"] != "approval_required"
    assert r["recommendation"] == "reject" and r["risk"] == "high" and "in_approved_range" in r["failed"], r["failed"]


def test_any_change_on_cell_a_is_protected_and_rejected():
    cfg, _, _ = outage_world()
    for parameter, value in (("tx_power_dbm", 44), ("tilt_deg", 5), ("enabled", 0)):
        r = run_check(proposal(cfg, "A", parameter, value, agent="availability"))
        assert r["checks"]["protected_cell"] is True, (parameter, r["checks"])
        assert r["recommendation"] == "reject" and "protected_cell" in r["failed"], (parameter, r["failed"])


def test_recent_agent_change_counts():
    ledger = [{"cell_id": "C", "by": "agent", "minutes_ago": 5}]
    assert rc.minutes_since_agent_change(ledger, "C") == 5
    cfg, _, _ = outage_world()
    r = run_check(proposal(cfg, "C", "enabled", 1), evidence={"C.enabled": 0}, ledger=ledger)
    assert r["checks"]["changed_recently"] is True and r["checks"]["minutes_since_last_change"] == 5
    assert r["recommendation"] == "reject" and "changed_recently" in r["failed"], r["failed"]


def test_operator_change_does_not_count():
    ledger = [{"cell_id": "C", "by": "operator", "minutes_ago": 5}]
    assert rc.minutes_since_agent_change(ledger, "C") == 9999
    cfg, _, _ = outage_world()
    r = run_check(proposal(cfg, "C", "enabled", 1), evidence={"C.enabled": 0}, ledger=ledger)
    assert r["checks"]["changed_recently"] is False and r["checks"]["minutes_since_last_change"] == 9999
    assert "changed_recently" not in r["failed"]
    # power-E-high's own ledger is the operator's injection 30 min ago: it must not start the clock either
    assert rc.minutes_since_agent_change(fault("power-E-high")["ledger"], "E") == 9999


def test_wrong_evidence_needs_more_evidence():
    cfg, _, _ = outage_world()
    r = run_check(proposal(cfg, "C", "enabled", 1), evidence={"C.enabled": 1})
    assert r["checks"]["evidence_matches_snapshot"] is False
    assert r["checks"]["mismatched_evidence"] == [{"key": "C.enabled", "cited": 1, "snapshot": 0}], \
        r["checks"]["mismatched_evidence"]
    assert r["recommendation"] == "needs_more_evidence", (r["recommendation"], r["failed"])


def test_unknown_evidence_key_is_a_mismatch():
    _, snap, _ = outage_world()
    bad = rc.evidence_mismatches({"C.no_such_kpi": 1, "network.phones_dropped": snap["network"]["phones_dropped"]}, snap)
    assert [b["key"] for b in bad] == ["C.no_such_kpi"], bad


def test_wrong_simulation_ref_is_invalid():
    cfg, _, _ = outage_world()
    for ref in ("SIM-0000000000000000", m.simulation_ref(m.default_config(), "C", "enabled", 1)):
        r = run_check(proposal(cfg, "C", "enabled", 1, ref=ref), evidence={"C.enabled": 0})
        assert r["checks"]["simulation_ref_valid"] is False, ref
        assert r["recommendation"] == "needs_more_evidence", (ref, r["recommendation"], r["failed"])


def test_rank_puts_more_agreeing_agents_first():
    cfg, _, _ = outage_world()
    p_one = proposal(cfg, "C", "enabled", 1, pid="PROP-availability-1", agent="availability")
    p_two = proposal(cfg, "G", "tx_power_dbm", 44, pid="PROP-coverage-1", agent="coverage")
    proposals = [p_one, p_two]
    reports = {"availability": {"findings": [{"cell_id": "C", "finding": "C is dark", "evidence": {"C.enabled": 0}}]},
               "coverage": {"findings": [{"cell_id": "G", "finding": "G edge is weak", "evidence": {}}]},
               "capacity": {"findings": [{"cell_id": "g", "finding": "G is loaded", "evidence": {}}]}}
    checks = {p["proposal_id"]: run_check(p, evidence={"C.enabled": 0}, proposals=proposals, reports=reports)
              for p in proposals}
    assert checks["PROP-availability-1"]["cross_agent"]["agreeing_agents"] == ["availability"]
    assert checks["PROP-coverage-1"]["cross_agent"]["agreeing_agents"] == ["capacity", "coverage"]
    assert rc.rank(proposals, checks) == ["PROP-coverage-1", "PROP-availability-1"]
    # agreement beats risk and phones left without service
    synthetic = {"P-few": {"cross_agent": {"agreeing_agents": ["coverage"]}, "risk": "low",
                           "predicted": {"phones_dropped_after": 0.0}},
                 "P-many": {"cross_agent": {"agreeing_agents": ["capacity", "coverage", "availability"]}, "risk": "medium",
                            "predicted": {"phones_dropped_after": 2.0}}}
    eligible = [{"proposal_id": "P-few", "proposed_value": 1, "current_value": 0},
                {"proposal_id": "P-many", "proposed_value": 46, "current_value": 43}]
    assert rc.rank(eligible, synthetic) == ["P-many", "P-few"]


# ---- 3. Cedar: an allow and a deny for every @id in policies/swarm.cedar --------------------------
def test_cedar_policy_ids_line_up_with_policies():
    statements = re.findall(r"^\s*(permit|forbid)\s*\(", POLICY_TEXT, re.M)
    assert len(POLICY_IDS) == len(set(POLICY_IDS)) == len(statements), (POLICY_IDS, statements)


def test_cedar_models_approved_only():
    ok = decide(agent("coverage"), "invoke_model", 'Model::"x"', {}, {"status": "approved"})
    assert ok == ("Allow", ["models-approved-only"], []), ok
    no = decide(agent("coverage"), "invoke_model", 'Model::"x"', {}, {"status": "withdrawn"})
    assert no[0] == "Deny" and no[1] == NO_PERMIT, no


def test_cedar_handoff_graph():
    for target in ("coverage", "capacity", "availability", "risk"):
        d = decide(agent("orchestrator"), "handoff", agent(target))
        assert d == ("Allow", ["handoff-graph"], []), (target, d)
    d = decide(agent("coverage"), "handoff", agent("capacity"))
    assert d[0] == "Deny" and d[1] == NO_PERMIT, d
    assert decide(agent("orchestrator"), "handoff", agent("orchestrator"))[0] == "Deny"


def test_cedar_read_tools():
    d = decide(agent("coverage"), "get_cell_state", NETWORK)
    assert d == ("Allow", ["read-tools"], []), d
    d = decide(agent("coverage"), "set_tower_power", NETWORK)
    assert d[0] == "Deny" and d[1] == NO_PERMIT, d
    # every tool contracts.AGENT_TOOLS hands an agent must be one Cedar lets it call
    good_dql = dt_tools.parse_dql("fetch logs, from: now()-30m")
    for role, names in contracts.AGENT_TOOLS.items():
        for name in names:
            d = decide(agent(role), name, NETWORK, good_dql if name == "dt_query" else {})
            assert d[0] == "Allow", (role, name, d)


def test_cedar_snapshot_tool():
    d = decide(agent("orchestrator"), "kpi_snapshot", NETWORK)
    assert d == ("Allow", ["snapshot-tool"], []), d
    d = decide(agent("orchestrator"), "write_kpi_snapshot", NETWORK)
    assert d[0] == "Deny" and "snapshot-tool" not in d[1], d


def test_cedar_dql_read_shape():
    ok = decide(agent("coverage"), "dt_query", NETWORK, dt_tools.parse_dql("fetch logs, from: now()-30m"))
    assert ok == ("Allow", ["dql-read-shape"], []), ok
    too_far = dt_tools.parse_dql("fetch logs, from: now()-3h")
    assert too_far["window_minutes"] == 180
    d = decide(agent("coverage"), "dt_query", NETWORK, too_far)
    assert d[0] == "Deny" and d[1] == NO_PERMIT, d
    writes = dt_tools.parse_dql("fetch logs | delete")
    assert writes["has_write"] is True
    d = decide(agent("coverage"), "dt_query", NETWORK, writes)
    assert d[0] == "Deny" and d[1] == NO_PERMIT, d
    d = decide(agent("coverage"), "dt_query", NETWORK, dt_tools.parse_dql("fetch bizevents, from: now()-30m"))
    assert d[0] == "Deny", d


def test_cedar_domain_ownership():
    d = decide(agent("availability"), "propose", 'Cell::"C"', {"parameter": "enabled"})
    assert d == ("Allow", ["domain-ownership"], []), d
    d = decide(agent("coverage"), "propose", 'Cell::"C"', {"parameter": "enabled"})
    assert d[0] == "Deny" and d[1] == NO_PERMIT, d
    for role in ("risk", "orchestrator"):
        assert decide(agent(role), "propose", 'Cell::"C"', {"parameter": "tx_power_dbm"})[0] == "Deny", role
    # Cedar and contracts.OWNS must agree on who owns what
    for role in contracts.SPECIALISTS:
        for parameter in contracts.PARAMETERS:
            allowed = decide(agent(role), "propose", 'Cell::"C"', {"parameter": parameter})[0] == "Allow"
            assert allowed == (parameter in contracts.OWNS[role]), (role, parameter, allowed)


def test_cedar_power_range_matches_risk_checks():
    for v in range(25, 52):  # Cedar and risk_checks must agree on the power limits (a +1 dB step is always in the envelope)
        denied_by_range = "power-range" in apply(new=v, old=v - 1)[1]
        assert denied_by_range == (not rc.in_range("tx_power_dbm", v)), v


# ---- 4. dt_tools.parse_dql ------------------------------------------------------------------------
def test_parse_dql():
    q = dt_tools.parse_dql("fetch logs, from: now()-30m | filter loglevel == \"ERROR\"")
    assert q == {"dql_verb": "fetch", "window_minutes": 30, "data_object": "logs", "has_write": False}, q
    q = dt_tools.parse_dql("fetch spans, from: now()-2h")
    assert q["window_minutes"] == 120 and q["data_object"] == "spans", q
    q = dt_tools.parse_dql("fetch dt.davis.problems | limit 5")
    assert q["window_minutes"] == 120 and q["data_object"] == "dt.davis.problems" and q["dql_verb"] == "fetch", q
    assert dt_tools.parse_dql("fetch events, from: now()-1d")["window_minutes"] == 1440
    assert dt_tools.parse_dql('fetch logs, from: "2026-01-01T00:00:00Z"')["window_minutes"] == 99999
    q = dt_tools.parse_dql("timeseries avg(dt.host.cpu.usage)")
    assert q["dql_verb"] == "timeseries" and q["data_object"] == "metrics", q
    assert dt_tools.parse_dql("fetch logs | delete")["has_write"] is True
    assert dt_tools.parse_dql("fetch logs, from: now()-30m")["has_write"] is False


# ---- 5. contracts validators ----------------------------------------------------------------------
def _report(**over):
    rep = {"domain_verdict": "issue_found",
           "findings": [{"cell_id": "C", "finding": "cell C transmits nothing", "evidence": {"C.enabled": 0}}],
           "proposals": [{"cell_id": "C", "parameter": "enabled", "proposed_value": 1, "confidence": 0.9,
                          "simulation_ref": "SIM-fd13f01e9679a72d", "rationale": "switch C back on"}]}
    rep.update(over)
    return rep


def test_report_problems():
    assert contracts.report_problems(_report()) == []
    assert contracts.report_problems(_report(domain_verdict="no_issue_in_domain", findings=[], proposals=[])) == []
    bad = contracts.report_problems(_report(domain_verdict="looks_fine"))
    assert bad and "domain_verdict" in bad[0], bad
    for value in (1.5, "1", True, None):
        p = dict(_report()["proposals"][0], proposed_value=value)
        assert contracts.report_problems(_report(proposals=[p])), f"proposed_value {value!r} must fail"
    p = _report()["proposals"][0]
    assert contracts.report_problems(_report(proposals=[p, p, p])) == ["at most 2 proposals"]
    assert contracts.report_problems(_report(proposals=[dict(p, parameter="frequency")]))


def test_assessments_and_recommendation_problems():
    good = {"proposal_id": "P1", "risk": "low", "recommendation": "approval_required", "reasons": ["ok"]}
    assert contracts.assessments_problems({"assessments": [good]}, ["P1"]) == []
    assert contracts.assessments_problems({"assessments": [good]}, ["P1", "P2"]) == ["no assessment for P2"]
    assert contracts.assessments_problems({"assessments": [dict(good, risk="tiny")]}, ["P1"])
    assert contracts.recommendation_problems({"chosen": None}, []) == []
    assert contracts.recommendation_problems({"chosen": "P1", "rationale": "best"}, ["P1"]) == []
    assert contracts.recommendation_problems({"chosen": "P9", "rationale": "best"}, ["P1"])
    assert contracts.recommendation_problems({"chosen": "P1"}, ["P1"]) == ["rationale is missing"]


def test_sha256_of_ignores_key_order():
    h = contracts.sha256_of({"a": 1, "b": [1, 2]})
    assert h == contracts.sha256_of({"b": [1, 2], "a": 1})
    assert h.startswith("sha256:") and len(h) == 7 + 64


# ---- 6. adapters.replay ---------------------------------------------------------------------------
def test_replay_outage_c_snapshot():
    net = ReplayNetwork("outage-C", "test", "t")
    assert net.cell("C")["enabled"] == 0 and net.cell("c")["enabled"] == 0
    assert net.snapshot["snapshot_id"] == "SNAP-test-1" and net.snapshot["taken_at"] == "t"
    conf = net.configuration("C")
    assert conf["desired"]["enabled"] == 0 and conf["observed"]["enabled"] == 0 and conf["in_sync"] is True, conf
    assert "error" in net.cell("Z")
    assert [n["cell_id"] for n in net.neighbors("C")] == [n["cell_id"] for n in net.cell("C")["neighbors"]]


def test_replay_simulate_delta():
    net = ReplayNetwork("outage-C", "test", "t")
    sim = net.simulate_delta("c", "enabled", 1)
    assert "error" not in sim, sim
    assert sim["current_value"] == 0 and sim["proposed_value"] == 1
    assert sim["simulation_ref"] == m.simulation_ref(net.config, "C", "enabled", 1)
    base = m.network_kpis(LAYOUT, m.default_config())["network"]["phones_dropped"]
    assert sim["predicted_network"]["phones_dropped"] == base
    sim = net.simulate_delta("G", "tx_power_dbm", 2)
    assert sim["current_value"] == 43 and sim["proposed_value"] == 45, sim
    assert "error" in net.simulate_delta("Z", "tx_power_dbm", 2)
    assert "error" in net.simulate_delta("G", "tx_power_dbm", "lots")


def test_replay_alarm_history():
    net = ReplayNetwork("outage-C", "test", "t")
    rows = net.alarm_history("C")
    assert rows, "outage-C has an alarm on C"
    assert rows[0]["kind"] == "alarm" and rows[0]["minutes_ago"] == 4, rows
    assert net.alarm_history("E") == [], "E's agent change was 300 min ago, outside the default 60 min"
    assert net.alarm_history("E", window_minutes=400)[0]["by"] == "agent"


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


# ---- 3b. the checkpoint path: decide() on a real SwarmCheckpoint (the cedar_decide tests above never build one) ------
def test_swarm_checkpoint_decide_uses_swarm_policy(tmp_path, monkeypatch):
    """A swarm run crashed with "evaluate() takes from 5 to 6 positional arguments but 7 were given": the base decide()
    passed policy_set positionally into SwarmCheckpoint.evaluate, which takes extras as keywords only."""
    from unittest.mock import MagicMock

    import checkpoint
    from agents.governance import SwarmCheckpoint

    monkeypatch.setattr(checkpoint.boto3, "client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(SwarmCheckpoint, "_next_seq", lambda self: self.seq + 1)
    monkeypatch.setattr(checkpoint, "ARIZE_KEY_FILE", tmp_path / "no-arize-key")
    for key in ("ARIZE_API_KEY", "DT_TRACES_TOKEN", "DT_PLATFORM_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    cp = SwarmCheckpoint("test-run", "0" * 31 + "1", tmp_path / "run")
    rec = cp.decide('Agent::"orchestrator"', "handoff", 'Agent::"coverage"', {})
    assert (rec["decision"], rec["policies"]) == ("Allow", ["handoff-graph"])
    assert "policy_set" not in rec  # swarm decisions keep their original record shape
