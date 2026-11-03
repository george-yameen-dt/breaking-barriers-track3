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


# ---- critical cells and the one trace id from the problem to the Viavi command ------------------------------------------
def test_critical_cells_not_hurt():
    good = rec(5, "verification", status="VERIFIED", phones_without_service_before=16.0, measured_after=12.0, critical_cells_hurt=[])
    assert check.critical_safe([good])[0]
    bad = rec(5, "verification", status="VERIFY_FAILED", phones_without_service_before=12.0, measured_after=12.0, critical_cells_hurt=["S2"])
    assert not check.critical_safe([bad])[0]
    assert not check.critical_safe([])[0]


def test_problem_to_command_follows_one_trace_id():
    import hashlib
    trace = hashlib.sha256(b"P-2611-1").hexdigest()[:32]
    start = rec(1, "run_start", problem_id="P-2611-1", trace_id=trace)
    applied = rec(3, "applied", cell_id="S4", parameter="tx_power_dbm", old_value=40, new_value=44, reply={"accepted": True},
                  command_reason=f"trace_id={trace} run_id=r agent=coverage")
    ok, why = check.problem_to_command([start, applied])
    assert ok and trace in why, why
    other = dict(applied, command_reason="trace_id=ffff run_id=r")
    assert not check.problem_to_command([start, other])[0], "the command carries a different trace id"
    wrong = dict(start, trace_id="0" * 32)
    assert not check.problem_to_command([wrong, applied])[0], "the trace id is not the one derived from the problem id"
    assert not check.problem_to_command([dict(start, problem_id=""), applied])[0]


def test_new_units_registered():
    assert {"critical_safe", "problem_to_command"} <= set(check.UNITS)
    assert not {"detect", "one_problem"} & set(check.UNITS), "the Dynatrace-attested checks were taken out of scope"
