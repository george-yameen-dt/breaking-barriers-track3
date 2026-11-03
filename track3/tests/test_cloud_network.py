"""Which network each AgentCore runtime builds: the orchestrator attaches to the live twin; the others get a frozen copy."""

from adapters.replay import ReplayNetwork
from adapters.twin import TwinNetwork
from agents import cloud
from test_twin import SCENARIO
from test_twin_attach import READ_AND_COMMAND, FakeLink


def test_the_orchestrator_attaches_to_the_live_twin_over_http(monkeypatch):
    link = FakeLink()
    monkeypatch.setattr(cloud, "HttpLink", lambda: link)
    net = cloud.network_for(None, {"network": "viavi", "twin_scenario": SCENARIO, "taken_at": "t", "fault": "outage-C"}, "run-1")
    assert isinstance(net, TwinNetwork) and net.snapshot["window_s"] == [420, 480]
    assert {o["op"] for o in link.ops} <= READ_AND_COMMAND


def test_the_other_runtimes_rebuild_the_same_snapshot_from_the_state(monkeypatch):
    link = FakeLink()
    monkeypatch.setattr(cloud, "HttpLink", lambda: link)
    payload = {"network": "viavi", "twin_scenario": SCENARIO, "taken_at": "t", "fault": "outage-C"}
    orchestrator = cloud.network_for(None, payload, "run-1")
    worker = cloud.network_for(None, {**payload, "twin_state": orchestrator.twin.state()}, "run-1")
    assert worker.snapshot["snapshot_sha256"] == orchestrator.snapshot["snapshot_sha256"]
    assert worker.apply("S4", "tx_power_dbm", 44, reason="x")["accepted"] is False


def test_without_a_twin_the_replay_network_is_used():
    net = cloud.network_for(None, {"fault": "outage-C", "taken_at": "2026-11-02T12:00:00Z"}, "run-1")
    assert isinstance(net, ReplayNetwork)


def test_the_remote_agents_carry_the_twin_state(monkeypatch):
    monkeypatch.setattr(cloud.boto3, "client", lambda *a, **k: None)
    r = cloud.RemoteAgents({"run_id": "r", "trace_id": "t" * 32, "fault": "x", "taken_at": "t", "network": "viavi",
                            "twin_scenario": SCENARIO, "twin_state": {"windows": []}})
    assert r.base["network"] == "viavi" and r.base["twin_state"] == {"windows": []} and r.base["twin_scenario"] == SCENARIO
