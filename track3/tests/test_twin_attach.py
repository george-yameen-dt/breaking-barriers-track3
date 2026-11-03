"""Attaching to a run someone else drives: the agents read it and send commands, and never start, advance or stop it."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from adapters.twin import SCENARIOS, RecordedTwin, TwinNetwork, open_frozen, open_twin
from adapters.twin_link import HttpLink
from test_twin import SCENARIO, SPEC

READ_AND_COMMAND = {"attach", "wait_for", "window", "events", "command"}


class FakeLink:
    """The link's operations over the saved run: the live run is at 8 minutes, the operator's poller advances it."""

    def __init__(self):
        self.rec = RecordedTwin(SCENARIOS / SPEC["recording"], SPEC["recording_first_timestamp"])
        self.ops = []
        self.sim_time = 485.0

    def run(self, op, **_):
        self.ops.append(op)
        if op["op"] == "attach":
            return {"test_id": "rsg-fake", "status": "RUNNING", "sim_time": self.sim_time, "first_timestamp": 1000.0}
        if op["op"] == "wait_for":
            return {"sim_time": op["to"], "status": "RUNNING"}
        if op["op"] == "window":
            if (op["t0"], op["t1"]) in self.rec.windows:
                return self.rec.window(op["t0"], op["t1"])
            return {**copy.deepcopy(self.rec.window(420, 480)), "t": [op["t0"], op["t1"]]}  # the saved run ends at 480 s
        if op["op"] == "events":
            return []
        if op["op"] == "command":
            self.sim_time = 500.0  # the run keeps moving while the command is sent
            return {"accepted": True, "command_type": op["command_type"]}
        raise AssertionError(f"an agent must not run {op['op']!r}")


def attached():
    link = FakeLink()
    return open_twin("attach", SCENARIO, "test", "t", link=link), link


def test_attached_twin_never_starts_or_advances():
    net, link = attached()
    assert {o["op"] for o in link.ops} <= READ_AND_COMMAND
    assert net.snapshot["window_s"] == [420, 480], "the newest full window of the run"


def test_attached_measure_waits_for_the_next_window():
    net, link = attached()
    snap = net.measure()
    waits = [o for o in link.ops if o["op"] == "wait_for"]
    assert waits and waits[-1]["to"] == 540 and snap["window_s"] == [480, 540]
    assert {o["op"] for o in link.ops} <= READ_AND_COMMAND


def test_attached_measure_reads_the_first_full_window_after_the_command():
    """The run's clock is ahead of the swarm's: the window right after the incident one may already be recorded, from
    before the command. The window that shows the change starts at the next boundary after the command (500 s -> 540 s)."""
    net, link = attached()
    net.apply("S4", "tx_power_dbm", 44, reason="trace_id=x")
    snap = net.measure()
    assert snap["window_s"] == [540, 600], snap["window_s"]
    assert [o for o in link.ops if o["op"] == "wait_for"][-1]["to"] == 600
    assert [(o["t0"], o["t1"]) for o in link.ops if o["op"] == "window"][-1] == (540, 600)


def test_attached_apply_sends_a_command():
    net, link = attached()
    reply = net.apply("S4", "tx_power_dbm", 44, reason="trace_id=x")
    cmd = [o for o in link.ops if o["op"] == "command"][0]
    assert reply["accepted"] and cmd["params"] == {"cell": "S4/B3/C1", "power": 44} and cmd["test_id"] == "rsg-fake"


def test_close_does_not_stop_the_operators_run():
    net, link = attached()
    net.close()
    assert "stop" not in {o["op"] for o in link.ops}


def test_frozen_twin_rebuilds_the_same_snapshot():
    net, _ = attached()
    frozen = open_frozen(net.twin.state(), SCENARIO, "test", "t")
    assert frozen.snapshot["snapshot_sha256"] == net.snapshot["snapshot_sha256"]
    assert isinstance(frozen, TwinNetwork) and frozen.source == net.source


def test_frozen_twin_refuses_commands():
    net, _ = attached()
    frozen = open_frozen(net.twin.state(), SCENARIO, "test", "t")
    assert frozen.apply("S4", "tx_power_dbm", 44, reason="x")["accepted"] is False


# ---- HttpLink over a fake Viavi MCP -------------------------------------------------------------------
CELL_CSV = ('tags,time,ues,thp_dl,prb_dl,prb_pct,qos,power,energy_w,pci,frequency,nrcgi\n'
            '"Viavi.Cell.Name=S4/B3/C1",0,13,40,50,100,100,40,339,7,1800,42\n')
UE_CSV = ('tags,time,rsrp_mean,rsrp_p10,sinr_mean,sinr_p10,rsrq_mean,samples\n'
          '"Viavi.Cell.Name=S4/B3/C1",0,-90,-110,10,2,-11,60\n')


class FakeMcp(BaseHTTPRequestHandler):
    calls = []
    active = True

    def log_message(self, *a):
        pass

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name, args = req["params"]["name"], req["params"]["arguments"]
        FakeMcp.calls.append((name, args))
        if name == "simulation_status":
            out = ({"test_id": "rsg-9", "status": "RUNNING", "sim_time": 120.0, "first_timestamp": 1000.0} if FakeMcp.active
                   else "Error executing tool simulation_status: no simulation is currently active")
            result = {"content": [{"text": json.dumps(out) if FakeMcp.active else out}], "isError": not FakeMcp.active}
        elif name == "simulation_query":
            q = args["query"]
            text = CELL_CSV if 'FROM "CellReports"' in q else UE_CSV if "GROUP BY" in q else "n,ue_thp_dl,ue_qos\n60,20,100\n"
            result = {"content": [{"text": json.dumps({"csv": text})}]}
        else:
            result = {"content": [{"text": json.dumps({"accepted": True, "echo": args})}]}
        body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def mcp():
    FakeMcp.calls, FakeMcp.active = [], True
    server = HTTPServer(("127.0.0.1", 0), FakeMcp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/mcp"
    server.shutdown()


def test_http_link_matches_local_link_ops(mcp):
    link = HttpLink(mcp)
    a = link.run({"op": "attach"})
    assert (a["test_id"], a["sim_time"], a["first_timestamp"]) == ("rsg-9", 120.0, 1000.0)
    w = link.run({"op": "window", "test_id": "rsg-9", "first": 1000.0, "t0": 60, "t1": 120})
    assert w["cells"]["S4/B3/C1"]["pci"] == 7.0 and w["cells"]["S4/B3/C1"]["rsrp_p10"] == -110.0
    c = link.run({"op": "command", "test_id": "rsg-9", "command_type": "tx_power", "params": {"cell": "S4/B3/C1", "power": 44},
                  "reason": "trace_id=x"})
    assert c["accepted"] is True and ("simulation_command", c["echo"]) in FakeMcp.calls


@pytest.mark.parametrize("op", ["start", "step", "stop", "slot"])
def test_http_link_refuses_lifecycle_ops(mcp, op):
    with pytest.raises(PermissionError):
        HttpLink(mcp).run({"op": op, "scenario": "x", "test_id": "t", "seconds": 60, "to": 60})
    assert not FakeMcp.calls, "a refused operation must not reach the Viavi MCP"


def test_http_link_refuses_when_no_active_run(mcp):
    FakeMcp.active = False
    with pytest.raises(RuntimeError, match="no simulation is currently active"):
        HttpLink(mcp).run({"op": "attach"})
