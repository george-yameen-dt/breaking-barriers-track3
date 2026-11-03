"""The 8-minute twin test, paced in real time: 3 healthy minutes, then S3 loses power at 180 s and stays dark.

One twin window per wall-clock minute, so Dynatrace gets a clean one-point-per-minute series (3 healthy, 5 fault):
each tower and the network as logs (service.name viavi-twin, with twin.phase and twin.minute), plus the twin's own
events. No agents: this is the operator's test run. The problem opens on Tower S3 a minute or two after the fault.

    VIAVI_RSG_URL=https://<rsg>/mcp python scenarios/twin/run8.py [--scenario team04-track3-4cell-powercut] [--minutes 8]

The twin has one run slot for the whole team: ping before running. The start refuses if another run is going
(never force), and the run is always stopped at the end. Windows and events also go to dev-runs/<run_id>/twin-8min.jsonl.
"""

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))
from adapters.twin import CELLS, PLANNED, REPORTS_PER_WINDOW, WINDOW_S, short, snapshot  # noqa: E402
from adapters.twin_dt import NETWORK, NETWORK_NAME, SERVICE, DynatraceLogs  # noqa: E402
from adapters.twin_link import TwinLink  # noqa: E402

FAULT_AT = 180


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="team04-track3-4cell-powercut")
    ap.add_argument("--minutes", type=int, default=8)
    ap.add_argument("--dt", choices=["send", "dry-run"], default="send")
    a = ap.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M") + "-twin-8min"
    trace_id = uuid.uuid4().hex
    out = HERE / "dev-runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    dt = DynatraceLogs(a.dt, run_id, trace_id, dry_file=out / "dynatrace-dry-run.jsonl")
    config = {c: {**PLANNED, "classification": "production", "maintenance_window": 0} for c in CELLS}
    link = TwinLink()
    test_id = link.run({"op": "start", "scenario": a.scenario})["test_id"]
    print(f"run {run_id} · trace {trace_id} · twin {test_id} · scenario {a.scenario}", flush=True)
    first, phones_active, seen = None, None, set()
    try:
        for m in range(1, a.minutes + 1):
            tick = time.monotonic()
            r = link.run({"op": "step", "test_id": test_id, "seconds": WINDOW_S, "to": m * WINDOW_S, "first": first})
            first, w = r["first"], r["window"]
            phones_active = phones_active or round(float(w["network"]["ue_samples"]) / REPORTS_PER_WINDOW, 1)
            phase = "healthy" if m * WINDOW_S <= FAULT_AT else "fault"
            snap = snapshot(w, config, phones_active, scenario=a.scenario)
            extra = {"twin.phase": phase, "twin.minute": m, "twin.test_id": test_id, "twin.scenario": a.scenario}
            dt.snapshot(snap, "live", extra=extra)
            events = [e for e in link.run({"op": "events", "test_id": test_id, "first": first, "until": m * WINDOW_S})
                      if (e["time"], e["event_type"], e["cell"]) not in seen]
            for e in events:
                seen.add((e["time"], e["event_type"], e["cell"]))
                c = short(e.get("cell"))
                dt._ship([{"service.name": SERVICE, "log.source": "breaking-barriers/track3", "trace_id": trace_id,
                           "twin.run_id": run_id, "twin.kind": "event", "loglevel": "WARN",
                           "twin.network": NETWORK, "twin.network_name": NETWORK_NAME, "twin.cell": c,
                           "twin.cell_name": f"Tower {c}", "twin.event_type": e["event_type"].replace("\\ ", " "),
                           "content": f"{c}: {e['event_type']}: {e.get('description')}", **extra}])
            n = snap["network"]
            towers = " ".join(f"{c['cell_id']}:{'on' if c['enabled'] else 'DARK'}/QoS {c['qos_score']}"
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
