"""The laptop's line to the Viavi AI RSG twin: SSM Run Command on the one host the RSG answers (Stan's EC2).

Each call sends a short Python program to that host (AWS-RunShellScript). It calls the RSG's MCP tools (JSON-RPC
tools/call, as viavi-proof/mcp.py does) and prints one JSON line back. Nothing is installed or left on the host.
Run control follows the twin SDK: start offline and paced, advance 60 s windows, read each window with InfluxQL,
always stop at the end.

Needs AWS credentials that may send SSM commands to the host, and VIAVI_RSG_URL (https://<rsg>/mcp).
"""

import json
import os
import shlex

import boto3
from botocore.exceptions import WaiterError

HOST = os.environ.get("TWIN_HOST", "i-06e27097bc0c6bef8")
REGION = "us-east-2"

# The host-side helpers (MCP call, wait for a pause, InfluxQL, 60 s window), shared by BOX and other host programs.
HELPERS = r'''
import csv, io, json, os, ssl, sys, time, urllib.request

URL = os.environ["VIAVI_RSG_URL"]
CTX = ssl.create_default_context()
CTX.check_hostname, CTX.verify_mode = False, ssl.CERT_NONE  # the RSG serves a self-signed certificate


def call(tool, **args):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    req = urllib.request.Request(URL, data=body.encode(), headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    res = json.load(urllib.request.urlopen(req, context=CTX, timeout=180)).get("result", {})
    text = "\n".join(c.get("text", "") for c in res.get("content", []))
    if res.get("isError") or res.get("is_error"):
        raise RuntimeError(f"{tool}: {text[:400]}")
    try:
        return json.loads(text)
    except ValueError:
        return text


def wait(test_id, sim_time, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = call("simulation_status", test_id=test_id)
        if "PAUSED" in str(s.get("status")) and float(s.get("sim_time") or 0) >= sim_time:
            return s
        time.sleep(1)
    raise TimeoutError(f"not paused at {sim_time}")


def query(test_id, q):
    r = call("simulation_query", test_id=test_id, query=q)
    if r.get("truncated"):
        raise RuntimeError(f"truncated: {r.get('note')}")
    return list(csv.DictReader(io.StringIO(r.get("csv", ""))))


def tag(row, key):
    for part in row.get("tags", "").split(","):
        if part.startswith(key + "="):
            return part.split("=", 1)[1]
    return row.get(key, "")


def f(v):
    return round(float(v), 2) if v not in ("", None) else None


def window(test_id, first, t0, t1):
    where = f"WHERE time >= {round(first + t0)}s AND time < {round(first + t1)}s"
    cells = {}
    for r in query(test_id, 'SELECT mean("RRC.ConnMean") AS ues, mean("DRB.UEThpDl") AS thp_dl, '
                            'mean("RRU.PrbUsedDl") AS prb_dl, mean("RRU.PrbTotDl") AS prb_pct, '
                            'mean("Viavi.QoS.Score") AS qos, last("Viavi.Radio.power") AS power, '
                            f'mean("PEE.AvgPower") AS energy_w FROM "CellReports" {where} GROUP BY "Viavi.Cell.Name"'):
        cells[tag(r, "Viavi.Cell.Name")] = {k: f(r.get(k)) for k in
                                            ("ues", "thp_dl", "prb_dl", "prb_pct", "qos", "power", "energy_w")}
    for r in query(test_id, 'SELECT mean("Viavi.UE.Rsrp") AS rsrp_mean, percentile("Viavi.UE.Rsrp", 10) AS rsrp_p10, '
                            'mean("Viavi.UE.RsSinr") AS sinr_mean, percentile("Viavi.UE.RsSinr", 10) AS sinr_p10, '
                            'mean("Viavi.UE.Rsrq") AS rsrq_mean, '
                            f'count("Viavi.UE.Rsrp") AS samples FROM "UEReports" {where} GROUP BY "Viavi.Cell.Name"'):
        cells.setdefault(tag(r, "Viavi.Cell.Name"), {}).update(
            {k: f(r.get(k)) for k in ("rsrp_mean", "rsrp_p10", "sinr_mean", "sinr_p10", "rsrq_mean", "samples")})
    net = query(test_id, f'SELECT count("Viavi.UE.Rsrp") AS n, mean("DRB.UEThpDl") AS ue_thp_dl, '
                         f'mean("Viavi.QoS.Score") AS ue_qos FROM "UEReports" {where}')
    covered = query(test_id, f'SELECT count("Viavi.UE.Rsrp") AS n FROM "UEReports" {where} AND "Viavi.UE.Rsrp" > -110')
    n = float(net[0]["n"]) if net and net[0].get("n") else 0
    c = float(covered[0]["n"]) if covered and covered[0].get("n") else 0
    return {"t": [t0, t1], "cells": cells,
            "network": {"ue_samples": n, "coverage_ratio_rsrp_gt_-110": round(c / n, 3) if n else None,
                        "ue_thp_dl_mean": f(net[0].get("ue_thp_dl")) if net else None,
                        "ue_qos_mean": f(net[0].get("ue_qos")) if net else None}}


def events(test_id, first, until):
    rows = query(test_id, f'SELECT "description", "reason" FROM "Events" WHERE time >= {round(first)}s '
                          f'AND time <= {round(first + until)}s AND "event_type" != \'handover\' '
                          f'AND "event_type" != \'reselection\' GROUP BY "event_type", "cell"')
    return [{"time": r.get("time"), "event_type": tag(r, "event_type"), "cell": tag(r, "cell"),
             "description": r.get("description"), "reason": r.get("reason")} for r in rows]
'''

BOX = HELPERS + r'''

op = json.loads(sys.argv[1])
try:
    if op["op"] == "start":
        r = call("simulation_start", scenario=op["scenario"], offline=True, paced=True)
        out = {"test_id": r.get("test_id"), "status": wait(r.get("test_id"), 0)}
    elif op["op"] == "step":
        call("simulation_advance", test_id=op["test_id"], run_for=f"{op['seconds']}s")
        s = wait(op["test_id"], op["to"])
        first = op.get("first") or float(s["first_timestamp"])
        out = {"first": first, "window": window(op["test_id"], first, op["to"] - op["seconds"], op["to"])}
    elif op["op"] == "events":
        out = events(op["test_id"], op["first"], op["until"])
    elif op["op"] == "command":
        out = call("simulation_command", test_id=op["test_id"], command_type=op["command_type"], params=op["params"],
                   reason=op["reason"])
    elif op["op"] == "stop":
        out = call("simulation_stop", test_id=op["test_id"])
    else:
        raise ValueError(f"unknown op {op['op']!r}")
    print(json.dumps({"ok": True, "result": out}))
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:500]}"}))
'''


class TwinLink:
    """One SSM command per twin operation, on the host the RSG answers."""

    def __init__(self, host: str = HOST, rsg_url: str | None = None, region: str = REGION):
        self.host, self.url = host, rsg_url or os.environ.get("VIAVI_RSG_URL", "")
        if not self.url:
            raise SystemExit("set VIAVI_RSG_URL=https://<rsg>/mcp (the twin's address) first")
        self.ssm = boto3.client("ssm", region_name=region)

    def run(self, op: dict, timeout_s: int = 600, program: str = BOX):
        script = (f"VIAVI_RSG_URL={shlex.quote(self.url)} python3 - {shlex.quote(json.dumps(op))} <<'TWIN_PY'\n"
                  f"{program}\nTWIN_PY\n")
        cid = self.ssm.send_command(InstanceIds=[self.host], DocumentName="AWS-RunShellScript", TimeoutSeconds=60,
                                    Comment=f"track3 twin {op['op']}",
                                    Parameters={"commands": [script], "executionTimeout": [str(timeout_s)]},
                                    )["Command"]["CommandId"]
        try:
            self.ssm.get_waiter("command_executed").wait(CommandId=cid, InstanceId=self.host,
                                                         WaiterConfig={"Delay": 2, "MaxAttempts": timeout_s // 2})
        except WaiterError:  # a failed command still has output worth reading
            pass
        out = self.ssm.get_command_invocation(CommandId=cid, InstanceId=self.host)["StandardOutputContent"]
        last = [line for line in out.splitlines() if line.strip()][-1:]
        reply = json.loads(last[0]) if last else {"ok": False, "error": "no output from the host"}
        if not reply.get("ok"):
            raise RuntimeError(f"twin {op['op']}: {reply.get('error')}")
        return reply["result"]
