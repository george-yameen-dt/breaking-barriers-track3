"""The twin test, paced in real time: by default the scenario as saved (3 healthy minutes, then S3 loses power at 180 s
and stays dark; 8 minutes), or the operator's timing: --healthy-minutes 5 --fault-minutes 20 moves the fault to 300 s
and runs 25 minutes, so the swarm's change reaches the cells and shows in Dynatrace while the fault is still on.

One twin window per wall-clock minute, so Dynatrace gets a clean one-point-per-minute series (healthy, then fault):
each tower and the network as logs (service.name viavi-twin, with twin.phase and twin.minute), plus the twin's own
events. The operator starts it; the swarm may attach and send a change, and the minutes after it show the change.
The problem opens on Tower S3 a minute or two after the fault.

Every value sent is the live run's own: the cells it reports, each cell's measured transmit power (Viavi.Radio.power),
the phones counted one by one (every phone seen in the run, and those with mean RSRP above -110 dBm this minute), the
tilt (the twin does not report it: the scenario's starting tilt, read from the RSG, changed by the run's O1 updates),
and twin.phase from the twin's own events (an anomaly raised or a cell shut down). Nothing is pinned to a plan.

    VIAVI_RSG_URL=https://<rsg>/mcp python scenarios/twin/run8.py [--scenario team04-track3-4cell-powercut] [--minutes 8]
        [--link ssm|local]   (local: on the EC2 itself; see README.md for the operator's viavi-run)
        [--config-version V] (stamped as twin.config_version on every record, so two runs compare)
        [--healthy-minutes H --fault-minutes F]  (the fault at H min, F more minutes; --minutes defaults to H + F)

With the timing flags, the run starts from the saved scenario's config with only its fault delay and duration changed
(validated first); the saved scenario itself is not changed, so viavi-reset and other runs are not affected.

The twin has one run slot for the whole team: the run checks it first and refuses if another run is going (never
force, never stops it), and its own run is always stopped at the end. No heartbeat: exactly one network record and one
per tower each minute. Windows and events also go to dev-runs/<run_id>/twin-8min.jsonl.
"""

import argparse
import copy
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))
from adapters.twin import WINDOW_S, discover, o1_settings, scenario_settings, short, snapshot  # noqa: E402
from adapters.twin_dt import NETWORK, NETWORK_NAME, SERVICE, DynatraceLogs  # noqa: E402
from adapters.twin_link import LocalLink, TwinLink  # noqa: E402


def faulted(events: list) -> bool:
    """The twin itself says so: it raised an anomaly or shut a cell down (at the scenario's own fault time)."""
    return any(e["event_type"] == "turn_off" or (e["event_type"].replace("\\ ", " ") == "Anomaly State Change"
                                                 and str(e.get("description", "")).startswith("raise")) for e in events)


def with_timing(cfg: dict, healthy_min: int, fault_min: int) -> dict:
    """The scenario's config with its faults moved so the first starts at healthy_min (later ones keep their offsets),
    and a duration long enough for fault_min more minutes."""
    cfg = copy.deepcopy(cfg)
    faults = [f for k in ("Power_Failures", "Radio_Anomalies", "Connectivity_Anomalies")
              for f in cfg["Anomalies"].get(k) or [] if "delay" in f]
    if not faults:
        raise SystemExit("the scenario has no fault to time: --healthy-minutes needs one")
    shift = healthy_min * WINDOW_S - min(float(f["delay"]) for f in faults)
    for f in faults:
        f["delay"] = int(float(f["delay"]) + shift)
    cfg["System"]["duration"] = max(int(cfg["System"].get("duration") or 0), (healthy_min + fault_min + 1) * WINDOW_S)
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="team04-track3-4cell-powercut")
    ap.add_argument("--minutes", type=int, help="minutes to run (default 8, or healthy + fault minutes)")
    ap.add_argument("--healthy-minutes", type=int, help="the fault starts after this many minutes (default: as saved)")
    ap.add_argument("--fault-minutes", type=int, help="minutes to run after the fault starts")
    ap.add_argument("--dt", choices=["send", "dry-run"], default="send")
    ap.add_argument("--link", choices=["ssm", "local"], default="ssm",
                    help="ssm: from a laptop through the EC2; local: on the EC2 itself, calling the RSG directly")
    ap.add_argument("--config-version", default="baseline",
                    help="the cell config this run tests (baseline: the scenario as saved, no agent change)")
    a = ap.parse_args()
    timed = a.healthy_minutes is not None or a.fault_minutes is not None
    if timed and (a.healthy_minutes is None or a.fault_minutes is None or a.healthy_minutes < 1 or a.fault_minutes < 1):
        ap.error("--healthy-minutes and --fault-minutes go together, each at least 1")
    a.minutes = a.minutes or (a.healthy_minutes + a.fault_minutes if timed else 8)

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M") + "-twin-8min"
    trace_id = uuid.uuid4().hex
    out = HERE / "dev-runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    dt = DynatraceLogs(a.dt, run_id, trace_id, dry_file=out / "dynatrace-dry-run.jsonl", heartbeat=False)
    link = LocalLink() if a.link == "local" else TwinLink()
    slot = link.run({"op": "slot"})
    if not slot["free"]:  # someone else's run: leave it alone (never force, never stop it)
        s = slot["status"]
        raise SystemExit(f"the twin's run slot is busy: {s.get('test_id')} · {s.get('status')} · sim_time "
                         f"{s.get('sim_time')} · not starting {a.scenario}")
    saved = link.run({"op": "scenario_get", "name": a.scenario})
    start = scenario_settings(saved)  # the settings the run starts with
    if timed:
        test_id = link.run({"op": "start", "scenario": a.scenario,
                            "config": with_timing(saved, a.healthy_minutes, a.fault_minutes)})["test_id"]
    else:
        test_id = link.run({"op": "start", "scenario": a.scenario})["test_id"]
    timing = f"fault at {a.healthy_minutes} min" if timed else "timing as saved"
    print(f"run {run_id} · trace {trace_id} · twin {test_id} · scenario {a.scenario} ({timing}, {a.minutes} min) · "
          f"config {a.config_version}", flush=True)
    first, seen = None, set()
    try:
        for m in range(1, a.minutes + 1):
            tick = time.monotonic()
            r = link.run({"op": "step", "test_id": test_id, "seconds": WINDOW_S, "to": m * WINDOW_S, "first": first})
            first, w = r["first"], r["window"]
            rows = link.run({"op": "events", "test_id": test_id, "first": first, "until": m * WINDOW_S})
            # everything below is this window's own data: the cells it reports, its measured power and phones, the
            # tilt from the scenario's start plus the run's O1 updates, and the phase from the twin's own events
            so_far = [e for e in rows if float(e["time"]) / 1000 - first < m * WINDOW_S]  # an event at t1 is the next window's
            live = o1_settings(so_far, start)
            config = {c: {"tilt_deg": live.get(c, {}).get("tilt_deg"), "classification": "production",
                          "maintenance_window": 0} for c in discover(w)}
            snap = snapshot(w, config, None, scenario=a.scenario)
            phase = "fault" if faulted(so_far) else "healthy"
            extra = {"twin.phase": phase, "twin.minute": m, "twin.test_id": test_id, "twin.scenario": a.scenario,
                     "twin.config_version": a.config_version,
                     "twin.o1_updates": sum(e["event_type"] == "O1_update" for e in so_far)}
            dt.snapshot(snap, "live", extra=extra)
            events = [e for e in rows if (e["time"], e["event_type"], e["cell"]) not in seen]
            for e in events:
                seen.add((e["time"], e["event_type"], e["cell"]))
                c = short(e.get("cell"))
                dt._ship([{"service.name": SERVICE, "log.source": "breaking-barriers/track3", "trace_id": trace_id,
                           "twin.run_id": run_id, "twin.kind": "event", "loglevel": "WARN",
                           "twin.network": NETWORK, "twin.network_name": NETWORK_NAME, "twin.cell": c,
                           "twin.cell_name": f"Tower {c}", "twin.event_type": e["event_type"].replace("\\ ", " "),
                           "content": f"{c}: {e['event_type']}: {e.get('description')}", **extra}])
            n = snap["network"]
            towers = " ".join(f"{c['cell_id']}:{'on' if c['enabled'] else 'DARK'}/{c['tx_power_dbm']} dBm/tilt "
                              f"{c['tilt_deg']}/QoS {c['qos_score']}"
                              for c in snap["cells"])
            print(f"min {m} {phase:7} coverage {n['coverage_pct']}% · {n['phones_dropped']} of {n['phones_active']} "
                  f"phones without service · QoS {n['qos_score']} · {towers} · events {len(events)}", flush=True)
            with (out / "twin-8min.jsonl").open("a") as f:
                f.write(json.dumps({"minute": m, "phase": phase, "window": w, "events": events}) + "\n")
            if m < a.minutes:
                time.sleep(max(0.0, 60 - (time.monotonic() - tick)))
    finally:
        link.run({"op": "stop", "test_id": test_id})
        dt.close()
        print(f"stopped {test_id} · Dynatrace records sent {dt.sent}, errors {dt.errors} · {out.relative_to(HERE)}")


if __name__ == "__main__":
    main()
