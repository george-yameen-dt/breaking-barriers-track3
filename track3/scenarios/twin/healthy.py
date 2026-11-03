"""The healthy twin scenario: the organisers' maveric_4cell sample (the RL kit's Viavi scenario, no anomalies) with lower
phone targets, so its QoS Score starts good. Stan's team04-track3-4cell-faults is the same sample plus two faults.

With the sample's targets (8 + 10 Mb/s per phone) the cells are full even before a fault: in run rsg-15 phones got
about 6 of 18 Mb/s and the average UE QoS Score was about 34. Edge cases start from this healthy copy and change one value.

    VIAVI_RSG_URL=https://<rsg>/mcp python scenarios/twin/healthy.py try 3,1 2,1 1.5,0.5   # test runs, nothing saved
    VIAVI_RSG_URL=https://<rsg>/mcp python scenarios/twin/healthy.py save 2,1              # validate, then save
    VIAVI_RSG_URL=https://<rsg>/mcp python scenarios/twin/healthy.py powercut 2,1          # edge case: + S3 power cut

A pair is the two service targets in Mb/s (video call, buffered video); the video call's guaranteed floor becomes half
its target. Everything runs on Stan's EC2 through adapters/twin_link.py, one twin run at a time: a try refuses to start
while another run is going, and always stops its own runs.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from adapters.twin_link import HELPERS, TwinLink  # noqa: E402

NAME = "team04-track3-4cell-healthy"
# Edge cases: the healthy copy plus one anomaly. The power cut is Stan's S3 failure, at 180 s (healthy-power-cut.json).
EDGES = {"powercut": ("team04-track3-4cell-powercut", "S3 commercial power failure at 180 s", {"Power_Failures": [{
    "name": "fault-S3-power-failure", "affected_cells": "S3/B3/C1", "affected_areas": "", "trigger_fm": False,
    "delay": 180, "duration": 0, "faultType": {"type": "commercialpowerfailure", "partial_outage_duration": 0,
                                               "tx_power_reduction_percentage": 0}}]})}
TRIALS = Path(__file__).with_name("healthy-trials.json")

PROGRAM = HELPERS + r'''
import hashlib

SAMPLE = "/home/ec2-user/ran-rl-sagemaker/config/viavi_scenarios/maveric_4cell.json"


def healthy(video, buffered):
    cfg = json.load(open(SAMPLE))
    flows = cfg["UE_Configuration"]["UE_Groups"][0]["serviceConfig"]
    if [s["qosId"] for s in flows] != [2, 6] or any(cfg["Anomalies"][k] for k in ("Radio_Anomalies", "Power_Failures")):
        raise ValueError("the sample is not the one this recipe expects (flows 5QI 2 + 6, no anomalies)")
    flows[0].update(targetTput_Mbps=video, gbrTput_Mbps=video / 2)
    flows[1].update(targetTput_Mbps=buffered)
    cfg["Scenario_Generation"]["Summary"] = "team04 healthy 4-cell"
    cfg["Scenario_Generation"]["Description"] = (
        f"Team04 Track 3 healthy baseline: organisers maveric_4cell sample, no anomalies, phone targets "
        f"{video}+{buffered} Mb/s (sample 8+10) so QoS Score starts good")
    return cfg


def busy():
    rows = call("simulation_list", limit=1).get("simulations", [])
    status = str(rows[0].get("status")) if rows else ""
    return f"{rows[0].get('test_id')} {status}" if "RUNNING" in status or "PAUSED" in status else ""


def stop(test_id):
    r = call("simulation_stop", test_id=test_id)
    deadline = time.monotonic() + 60
    while not (isinstance(r, dict) and r.get("stopped")) and time.monotonic() < deadline:
        time.sleep(2)
        r = {"stopped": "STOPPED" in str(call("simulation_status", test_id=test_id).get("status"))}


def measure(test_id, first, t0, t1):
    where = f"WHERE time >= {round(first + t0)}s AND time < {round(first + t1)}s"
    for _ in range(15):  # an offline run writes its reports in batches, so a window can lag a few seconds
        rows = query(test_id, 'SELECT mean("Viavi.QoS.Score") AS qos, percentile("Viavi.QoS.Score", 10) AS qos_p10, '
                              'mean("DRB.UEThpDl") AS thp, mean("Viavi.UE.targetThroughputDl") AS target, '
                              f'count("Viavi.QoS.Score") AS n FROM "UEReports" {where}')
        if rows and float(rows[0].get("n") or 0):
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"no UE reports for {t0}-{t1}s")
    ue = rows[0]
    full = query(test_id, f'SELECT count("Viavi.QoS.Score") AS n FROM "UEReports" {where} AND "Viavi.QoS.Score" >= 99.9')
    covered = query(test_id, f'SELECT count("Viavi.UE.Rsrp") AS n FROM "UEReports" {where} AND "Viavi.UE.Rsrp" > -110')
    n = float(ue.get("n") or 0)
    share = lambda rows: round(float(rows[0]["n"]) / n, 3) if n and rows and rows[0].get("n") else 0.0
    cells = {}
    for r in query(test_id, 'SELECT mean("Viavi.QoS.Score") AS qos, mean("RRU.PrbUsedDl") AS prb, '
                            f'mean("RRC.ConnMean") AS ues FROM "CellReports" {where} GROUP BY "Viavi.Cell.Name"'):
        cells[tag(r, "Viavi.Cell.Name").split("/")[0]] = {k: f(r.get(k)) for k in ("qos", "prb", "ues")}
    return {"t": [t0, t1], "ue_qos": f(ue.get("qos")), "ue_qos_p10": f(ue.get("qos_p10")), "share_qos_100": share(full),
            "thp_mbps": f(ue.get("thp")), "target_mbps": f(ue.get("target")), "covered": share(covered), "cells": cells}


def trial(cfg, seconds):
    running = busy()
    if running:
        raise RuntimeError(f"another run is going ({running}); try again when it stops")
    test_id = call("simulation_start", config=cfg, offline=True, paced=True).get("test_id")
    try:
        wait(test_id, 0)
        call("simulation_advance", test_id=test_id, run_for=f"{seconds}s")
        first = float(wait(test_id, seconds)["first_timestamp"])  # set once reports exist, as twin_link's step reads it
    finally:
        stop(test_id)
    return {"test_id": test_id, "windows": [measure(test_id, first, t, t + 60) for t in range(0, seconds, 60)]}


op = json.loads(sys.argv[1])
try:
    if op["op"] == "try":
        out = []
        for video, buffered in op["targets"]:
            cfg = healthy(video, buffered)
            check = call("scenario_validate", config=cfg)
            out.append({"targets": [video, buffered], "valid": check.get("valid"),
                        **(trial(cfg, op["seconds"]) if check.get("valid") else {"errors": str(check)[:400]})})
    elif op["op"] == "save":
        video, buffered = op["targets"]
        cfg = healthy(video, buffered)
        if op.get("anomalies"):
            cfg["Anomalies"].update(op["anomalies"])
            cfg["Scenario_Generation"]["Description"] += "; edge case: " + op["edge"]
        check = call("scenario_validate", config=cfg)
        if not check.get("valid"):
            raise ValueError(f"invalid: {str(check)[:400]}")
        saved = call("scenario_save", name=op["name"], config=cfg, overwrite=False)
        out = {"saved": saved, "summary": check.get("summary"),
               "config_sha256": hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    else:
        raise ValueError(f"unknown op {op['op']!r}")
    print(json.dumps({"ok": True, "result": out}))
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {str(e)[:500]}"}))
'''


def show(trials: list) -> None:
    for t in trials:
        print(f"targets {t['targets'][0]}+{t['targets'][1]} Mb/s · {t.get('test_id', 'not run: ' + t.get('errors', ''))}")
        for w in t.get("windows", []):
            towers = " · ".join(f"{c} {v['qos']:.0f} (PRB {v['prb']:.0f}, {v['ues']:.0f} phones)"
                                for c, v in sorted(w["cells"].items()))
            print(f"  {w['t'][0]:>3}-{w['t'][1]:<3}s UE QoS {w['ue_qos']} (p10 {w['ue_qos_p10']}), "
                  f"{w['share_qos_100']:.0%} at 100, {w['thp_mbps']} of {w['target_mbps']} Mb/s, "
                  f"covered {w['covered']:.0%} | {towers}")


def main(argv: list) -> None:
    if not argv or argv[0] not in ("try", "save", *EDGES):
        raise SystemExit(__doc__)
    pairs = [[float(x) for x in a.split(",")] for a in argv[1:]] or [[3, 1], [2, 1], [1.5, 0.5]]
    link = TwinLink()
    if argv[0] == "try":
        trials = link.run({"op": "try", "targets": pairs, "seconds": 180}, timeout_s=900, program=PROGRAM)
        TRIALS.write_text(json.dumps(trials, indent=1) + "\n")
        show(trials)
    else:
        if len(pairs) != 1:
            raise SystemExit(f"{argv[0]} takes exactly one pair, e.g. {argv[0]} 2,1")
        name, edge, anomalies = EDGES.get(argv[0], (NAME, "", {}))
        print(json.dumps(link.run({"op": "save", "name": name, "targets": pairs[0], "edge": edge, "anomalies": anomalies},
                                  program=PROGRAM), indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
