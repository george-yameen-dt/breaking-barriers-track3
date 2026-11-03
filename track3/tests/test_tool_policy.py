"""Tools are callable only when registered and allowed: read-only Dynatrace, and a short allow-list of Viavi calls."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import checkpoint
import dt_tools
from agents.governance import SwarmCheckpoint
from checkpoint import Refused
from test_twin import twin_net

TRACK3 = Path(__file__).resolve().parent.parent
ORCH = 'Agent::"orchestrator"'
APPROVED = {"dtctl", "viavi:simulation_command", "viavi:simulation_query", "viavi:scenario_list", "viavi:simulation_start"}
TRACE = "0" * 31 + "1"


@pytest.fixture
def cp(tmp_path, monkeypatch):
    monkeypatch.setattr(checkpoint.boto3, "client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(SwarmCheckpoint, "_next_seq", lambda self: self.seq + 1)
    monkeypatch.setattr(checkpoint, "ARIZE_KEY_FILE", tmp_path / "no-arize-key")
    for key in ("ARIZE_API_KEY", "DT_TRACES_TOKEN", "DT_PLATFORM_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    c = SwarmCheckpoint("test-run", TRACE, tmp_path / "run")
    monkeypatch.setattr(c, "tool_status", lambda tool: "approved" if tool in APPROVED else "unregistered")
    return c


def command(cp, command_type, who=ORCH, delay_s=0, trace=True):
    return cp.gate_tool(who, "viavi_command", "viavi:simulation_command",
                        {"command_type": command_type, "delay_s": delay_s, "reason_has_trace_id": trace})


def test_unregistered_tool_denied(cp):
    with pytest.raises(Refused):
        cp.gate_tool(ORCH, "viavi_read", "viavi:simulation_status", {})


def test_denial_names_the_registry_rule(cp):
    d = cp.decide(ORCH, "viavi_read", 'Tool::"viavi:simulation_status"', {}, {"status": "unregistered"})
    assert (d["decision"], d["policies"]) == ("Deny", ["tools-approved-only"])


def test_dtctl_delete_denied(cp):
    with pytest.raises(Refused):
        dt_tools.dtctl_read(["delete", "dashboard", "abc"], cp)


def test_dtctl_query_allowed(cp, monkeypatch):
    out = SimpleNamespace(stdout=json.dumps({"ok": True, "result": {"records": [{"a": 1}]}}), stderr="")
    monkeypatch.setattr(dt_tools.subprocess, "run", lambda *a, **k: out)
    reply = dt_tools.dtctl_read(["query", "fetch logs | limit 1"], cp)
    assert reply["rows"] == [{"a": 1}]


def test_viavi_raw_denied(cp):
    with pytest.raises(Refused):
        command(cp, "raw")


@pytest.mark.parametrize("command_type", ["turn_off", "turn_on", "handover", "cho_override", "energy_saving",
                                           "cell_sleep_mode", "antenna_mask", "slice_prb_quotas"])
def test_viavi_other_commands_denied(cp, command_type):
    with pytest.raises(Refused):
        command(cp, command_type)


@pytest.mark.parametrize("command_type", ["tx_power", "antenna_tilt"])
def test_viavi_tx_power_allowed(cp, command_type):
    rec = command(cp, command_type)
    assert (rec["decision"], rec["policies"]) == ("Allow", ["viavi-command-allowlist"])


def test_viavi_command_without_trace_id_denied(cp):
    with pytest.raises(Refused):
        command(cp, "tx_power", trace=False)
    d = cp.decide(ORCH, "viavi_command", 'Tool::"viavi:simulation_command"',
                  {"command_type": "tx_power", "delay_s": 0, "reason_has_trace_id": False}, {"status": "approved"})
    assert "viavi-trace-id-in-reason" in d["policies"]


def test_viavi_long_delay_denied(cp):
    with pytest.raises(Refused):
        command(cp, "tx_power", delay_s=600)


def test_only_the_orchestrator_sends_commands(cp):
    with pytest.raises(Refused):
        command(cp, "tx_power", who='Agent::"coverage"')


def test_viavi_simulation_start_denied(cp):
    """Registered is not enough: starting a run is not on the allow-list."""
    with pytest.raises(Refused):
        cp.gate_tool(ORCH, "viavi_read", "viavi:simulation_start", {})


def test_viavi_read_allowed(cp):
    rec = cp.gate_tool('Agent::"coverage"', "viavi_read", "viavi:simulation_query", {})
    assert (rec["decision"], rec["policies"]) == ("Allow", ["viavi-read"])


def test_tools_json_matches_the_policy():
    tools = json.loads((TRACK3 / "policies" / "tools.json").read_text())
    policy = (TRACK3 / "policies" / "swarm.cedar").read_text()
    permitted = set(re.findall(r'Tool::"(viavi:[a-z_]+)"', policy.split('@id("viavi-read")')[1].split(";")[0]))
    reads = {f"viavi:{t}" for t in tools["viavi"] if t != "simulation_command"}
    assert reads == permitted, (reads ^ permitted)
    for denied in ("simulation_start", "simulation_stop", "simulation_advance", "scenario_save", "scenario_update",
                   "scenario_clone", "scenario_delete"):
        assert denied not in tools["viavi"]


def test_apply_is_gated_by_cedar(cp):
    net = twin_net()
    net.cp = cp
    reply = net.apply("S4", "tx_power_dbm", 44, reason="no trace id here")
    assert reply["accepted"] is False and "Cedar" in reply["error"] and not net.twin.sent
    reply = net.apply("S4", "tx_power_dbm", 44, reason=f"trace_id={TRACE} run_id=test-run agent=coverage")
    assert reply["accepted"] is True and net.twin.sent[0]["params"] == {"cell": "S4/B3/C1", "power": 44}
