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


def test_a_refused_attach_is_a_clean_refusal_not_a_crash(monkeypatch):
    from unittest.mock import MagicMock
    from contracts import Refused

    def refuse(cp, payload, run_id):
        raise Refused('Agent::"orchestrator" may not use viavi:simulation_query: tools-approved-only')

    cp = MagicMock()
    cp.run_id = "r"
    cp.policy_pin.return_value = None  # nothing pinned yet
    monkeypatch.setattr(cloud, "network_for", refuse)
    out = cloud._handle(cp, "swarm", "orchestrator", {"fault": "outage-C", "taken_at": "t"})
    assert out["refused"] is True and "tools-approved-only" in out["error"]


def test_a_swarm_run_under_an_unapproved_policy_is_refused_before_it_touches_the_network(monkeypatch):
    from unittest.mock import MagicMock

    cp = MagicMock()
    cp.run_id = "r"
    cp.policy_pin.return_value = "sha256:" + "0" * 64  # a risk owner pinned some other file
    monkeypatch.setattr(cloud, "network_for", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the network was touched")))
    out = cloud._handle(cp, "swarm", "orchestrator", {"fault": "outage-C", "taken_at": "t"})
    assert out["refused"] is True and "approved" in out["error"]
    assert [c for c in cp.record.call_args_list if c.args and c.args[0] == "policy_pin_checked"], "the mismatch is on the record"


def test_a_worker_runtime_does_not_check_the_pin_itself(monkeypatch):
    """Only the orchestrator's run start checks the pin; the workers are started by it."""
    from unittest.mock import MagicMock

    cp = MagicMock()
    cp.run_id = "r"
    cp.policy_pin.return_value = "sha256:" + "0" * 64
    monkeypatch.setattr(cloud, "network_for", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("past the pin check")))
    out = cloud._handle(cp, "specialist", "capacity", {"fault": "outage-C", "taken_at": "t"})
    assert "past the pin check" in out["error"]


def test_run_start_is_the_first_record_of_an_orchestrator_run(monkeypatch):
    """The trace check wants the run to begin with run_start, even when the first thing that happens is a refusal."""
    from unittest.mock import MagicMock

    from contracts import Refused

    cp = MagicMock()
    cp.run_id = "r"
    cp.policy_pin.return_value = None
    monkeypatch.setattr(cloud, "network_for", lambda *a, **k: (_ for _ in ()).throw(Refused("no")))
    cloud._handle(cp, "swarm", "orchestrator", {"fault": "outage-C", "taken_at": "t", "network": "viavi", "twin_scenario": SCENARIO})
    kinds = [c.args[0] for c in cp.record.call_args_list]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end", kinds


def test_a_pin_mismatch_still_ends_the_run_on_the_record(monkeypatch):
    from unittest.mock import MagicMock

    cp = MagicMock()
    cp.run_id = "r"
    cp.policy_pin.return_value = "sha256:" + "0" * 64
    out = cloud._handle(cp, "swarm", "orchestrator", {"fault": "outage-C", "taken_at": "t"})
    kinds = [c.args[0] for c in cp.record.call_args_list]
    assert out["refused"] and kinds == ["run_start", "policy_pin_checked", "run_end"], kinds
