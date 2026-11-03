"""The checks for an autonomous run: a pass and a fail for each unit, on hand-built record lists."""

from tools import check


def rec(seq, kind, **f):
    return {"seq": seq, "kind": kind, **f}


ALLOW = rec(2, "decision", action="apply_cell_configuration", decision="Allow", policies=["apply-autonomous-within-envelope"])
APPLIED = rec(3, "applied", cell_id="S4", parameter="tx_power_dbm", old_value=40, new_value=44, reply={"accepted": True})
START = rec(1, "run_start", policies_sha256="abc")


def test_check_autonomous_apply_pass_and_fail():
    ok, why = check.swarm_autonomous_apply([START, ALLOW, APPLIED])
    assert ok and "40 -> 44" in why, why
    assert not check.swarm_autonomous_apply([START, APPLIED])[0], "an applied record without an Allow before it fails"
    asked = rec(2, "approval_requested")
    assert not check.swarm_autonomous_apply([START, asked, ALLOW, APPLIED])[0], "a network approval request fails the unit"
    refused = rec(3, "applied", cell_id="S4", parameter="tx_power_dbm", old_value=40, new_value=44, reply={"accepted": False})
    assert not check.swarm_autonomous_apply([START, ALLOW, refused])[0], "a refused change is not an applied change"


def test_check_verify_revert_path():
    ok_v = rec(4, "verification", status="VERIFIED", phones_without_service_before=16.0, measured_after=12.0)
    assert check.swarm_verify([APPLIED, ok_v])[0]
    bad_v = rec(4, "verification", status="VERIFY_FAILED", phones_without_service_before=12.0, measured_after=16.0)
    reverted = rec(5, "reverted", cell_id="S4", parameter="tx_power_dbm", restored_value=40)
    assert check.swarm_verify([APPLIED, bad_v, reverted])[0]
    assert not check.swarm_verify([APPLIED, bad_v])[0], "a failed verification with no undo fails"
    assert not check.swarm_verify([APPLIED])[0], "no verification fails"


def test_units_are_registered():
    assert {"swarm_autonomous_apply", "swarm_verify"} <= set(check.UNITS)
