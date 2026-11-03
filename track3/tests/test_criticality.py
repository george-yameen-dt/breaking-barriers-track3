"""Critical cells: the registry, the risk check that reads it, and the Cedar rules that protect those cells."""

import re
from pathlib import Path
from types import SimpleNamespace

import cedarpy

import risk_checks as rc
from adapters import criticality as crit
from agents.governance import cedar_decide
from test_twin import chosen, twin_net

POLICIES = Path(__file__).resolve().parent.parent / "policies"
TEXT = (POLICIES / "swarm.cedar").read_text()
IDS = re.findall(r'@id\("([^"]+)"\)', TEXT)
ORCH = 'Agent::"orchestrator"'
REG = {"S2/B3/C1": {"class": "hospital", "name": "Hospital (synthetic)"}, "S4/B3/C1": {"class": "emergency", "name": "Dispatch (synthetic)"}}


def apply(parameter="tx_power_dbm", old=40, new=44, **over):
    ctx = {"proposal_id": "P", "parameter": parameter, "old_value": old, "new_value": new, "delta": new - old,
           "abs_delta": abs(new - old), "risk_checks_passed": True, "selected_proposal_matches": True, "applies_this_run": 0,
           "minutes_since_last_change": 9999, "criticality": "none", "critical_harm_predicted": False, **over}
    decision, policies, errors = cedar_decide(TEXT, IDS, ORCH, "apply_cell_configuration", 'Cell::"S4"', ctx)
    assert not errors, errors
    return decision, policies


def test_unknown_cell_is_none():
    assert crit.criticality_of("S9/B3/C1", registry={}) == "none"
    assert crit.criticality_of("not a cell", registry=REG) == "none"


def test_short_and_full_names_match():
    assert crit.criticality_of("S2", registry=REG) == crit.criticality_of("S2/B3/C1", registry=REG) == "hospital"


def test_default_registry_has_a_hospital_and_a_dispatch_centre():
    reg = crit.load()
    assert {v["class"] for v in reg.values()} == {"hospital", "emergency"}
    assert crit.criticality_of("S2") == "hospital" and crit.criticality_of("S4") == "emergency"
    assert crit.criticality_of("S1") == crit.criticality_of("S3") == "none"


def test_critical_power_cut_denied():
    assert apply(old=40, new=38, criticality="hospital") == ("Deny", ["critical-no-power-cut"])
    assert apply(old=40, new=38, criticality="none") == ("Allow", ["apply-autonomous-within-envelope"])


def test_critical_tilt_up_denied():
    assert apply("tilt_deg", 6, 8, criticality="emergency") == ("Deny", ["critical-no-tilt-up"])
    assert apply("tilt_deg", 6, 4, criticality="emergency") == ("Allow", ["apply-autonomous-within-envelope"])


def test_critical_power_plus_5_denied_plus_4_allowed():
    assert apply(old=40, new=45, criticality="emergency") == ("Deny", ["critical-power-step-4db"])
    assert apply(old=40, new=44, criticality="emergency") == ("Allow", ["apply-autonomous-within-envelope"])


def test_critical_harm_denies_neighbour_fix():
    assert apply(criticality="none", critical_harm_predicted=True) == ("Deny", ["critical-neighbour-no-harm"])


def test_revert_on_a_critical_cell_stays_allowed():
    ctx = {"proposal_id": "P", "ledger_status": "VERIFY_FAILED", "restores_old_value": True}
    d, p, e = cedar_decide(TEXT, IDS, ORCH, "revert_cell_configuration", 'Cell::"S4"', ctx)
    assert (d, p, e) == ("Allow", ["revert-exact"], [])


def fake_net(sinr_s2, current=40):
    cells = [{"cell_id": "S2", "enabled": 1, "sinr_db_p10": sinr_s2}, {"cell_id": "S4", "enabled": 1, "sinr_db_p10": 4.0},
             {"cell_id": "S1", "enabled": 1, "sinr_db_p10": -9.0}]
    return SimpleNamespace(snapshot={"cells": cells}, config={"S4": {"tx_power_dbm": current, "tilt_deg": 6}})


def test_more_interference_near_a_weak_critical_cell_is_harm():
    assert rc.critical_harm(fake_net(-5.0), "S4", "tx_power_dbm", 44, registry=REG) is True
    assert rc.critical_harm(fake_net(10.0), "S4", "tx_power_dbm", 44, registry=REG) is False
    assert rc.critical_harm(fake_net(-5.0), "S4", "tx_power_dbm", 38, registry=REG) is False, "less power is not more interference"
    assert rc.critical_harm(fake_net(-5.0), "S4", "tx_power_dbm", 44, registry={}) is False, "no critical cell, no harm"


def test_unknown_cell_in_the_snapshot_does_not_crash_the_harm_check():
    net = fake_net(-5.0)
    net.snapshot["cells"].append({"cell_id": "S77", "enabled": 1, "sinr_db_p10": None})
    assert rc.critical_harm(net, "S4", "tx_power_dbm", 44, registry=REG) is True


def test_risk_check_lists_criticality():
    net = twin_net()
    p, _ = chosen(net)
    p["simulation_ref"], p["confidence"] = net.simulate("S4", "tx_power_dbm", 44)["simulation_ref"], 0.8
    c = rc.check(p, {"S4.dropped_nearby": 8.7}, net.snapshot, net.layout, net.config, net.ledger, [p], {},
                 simulate=net.simulate, criticality="emergency", critical_harm=False)
    assert c["criticality"] == "emergency" and c["checks"]["critical_cell_ok"] and c["checks"]["critical_neighbour_ok"]
    assert (c["risk"], c["recommendation"]) == ("low", "approval_required")
    cut = dict(p, parameter="tx_power_dbm", proposed_value=38)
    c = rc.check(cut, {}, net.snapshot, net.layout, net.config, net.ledger, [cut], {}, simulate=net.simulate,
                 criticality="hospital", critical_harm=False)
    assert "critical_cell_ok" in c["failed"] and c["recommendation"] == "reject"
    c = rc.check(p, {"S4.dropped_nearby": 8.7}, net.snapshot, net.layout, net.config, net.ledger, [p], {},
                 simulate=net.simulate, criticality="none", critical_harm=True)
    assert "critical_neighbour_ok" in c["failed"] and c["recommendation"] == "reject"


def test_the_snapshot_names_each_cells_criticality():
    cells = {c["cell_id"]: c["criticality"] for c in twin_net().snapshot["cells"]}
    assert cells == {"S1": "none", "S2": "hospital", "S3": "none", "S4": "emergency"}


def test_policies_still_validate_against_the_schema():
    result = cedarpy.validate_policies(TEXT, (POLICIES / "swarm.cedarschema").read_text())
    assert result.validation_passed, [str(e) for e in result.errors]


def snap_with(qos_s2, qos_s4):
    return {"cells": [{"cell_id": "S2", "criticality": "hospital", "qos_score": qos_s2},
                      {"cell_id": "S4", "criticality": "emergency", "qos_score": qos_s4},
                      {"cell_id": "S1", "criticality": "none", "qos_score": 100.0}]}


def test_a_critical_cell_that_loses_qos_after_a_change_is_hurt():
    from agents.orchestrator import critical_cells_hurt
    assert critical_cells_hurt(snap_with(100.0, 28.0), snap_with(100.0, 60.0)) == []
    assert critical_cells_hurt(snap_with(100.0, 60.0), snap_with(80.0, 60.0)) == ["S2"]
    assert critical_cells_hurt(snap_with(100.0, 60.0), snap_with(97.0, 60.0)) == [], "a 3 point wobble is not harm"


def test_a_worse_critical_cell_fails_verification_and_is_undone():
    import copy
    from types import SimpleNamespace

    from agents.orchestrator import apply_and_verify
    from test_twin import FakeCheckpoint, WorseTwin  # noqa: F401  (WorseTwin: phones without service worse)

    net, cp = twin_net(), FakeCheckpoint()
    for c in net.snapshot["cells"]:  # the saved run has no per-cell QoS score; give every cell a healthy one
        c["qos_score"] = 100.0
    seen = {}
    real = net.measure

    def measure():
        snap = copy.deepcopy(real())
        for c in snap["cells"]:
            if c["cell_id"] == "S2":
                c["qos_score"] = 80.0
            else:
                c["qos_score"] = 100.0
        seen["after"] = snap
        return snap

    net.measure = measure
    run = SimpleNamespace(cp=cp, net=net)
    p, ctx = chosen(net)
    outcome = apply_and_verify(run, p, ctx, "16.0 -> 14.6", say=lambda s: None)
    v = next(r for r in cp.records if r["kind"] == "verification")
    assert v["status"] == "VERIFY_FAILED" and v["critical_cells_hurt"] == ["S2"], v
    assert "undone" in outcome, outcome
