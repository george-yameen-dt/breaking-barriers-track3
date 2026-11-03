"""Put the team's Viavi scenarios back to the demo baseline, so every demo run starts the same way.

The baseline (baseline-4cell-powercut.json, read from the RSG on 3 Nov 2026): the four cells S1 to S4 at 40 dBm and
tilt 6, and S3 loses commercial power at 180 s, with no other anomaly. Coverage is about 77 % for the first three
minutes and about 65 % from minute 4, so Dynatrace opens the problem below 70 %.

    python scenarios/twin/reset.py            # rewrite both scenarios from the baseline, then read them back
    python scenarios/twin/reset.py --check    # compare only, write nothing (exit 1 if a scenario has drifted)
    python scenarios/twin/reset.py --verify   # also a fast run of team04-track3-live: >= 70 % for 3 min, then < 70 %

Runs on the EC2 (~/team04-track3/viavi-reset), next to viavi-run. The agents' tx_power and antenna_tilt commands
change only the run they are sent to, and the saved scenario does not change. A fresh viavi-run already starts at
40 dBm. The reset is for everything else: a scenario edited in the UI, a test save, or a scenario that was overwritten.
Writing and --verify need the twin's one run slot to be free (never force, never stop another run). --verify sends
nothing to Dynatrace.
"""

import argparse
import copy
import csv
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))
from adapters.twin import PLANNED, WINDOW_S  # noqa: E402
from adapters.twin_link import LocalLink  # noqa: E402

BASELINE = Path(__file__).with_name("baseline-4cell-powercut.json")
# The scenarios the demo runs, each the baseline with these differences
TARGETS = {"team04-track3-4cell-powercut": {"duration": 600, "summary": "team04 healthy 4-cell"},
           "team04-track3-live": {"duration": 1200, "summary": "team04-track3-live"}}
VERIFY_SCENARIO = "team04-track3-live"
FAULT = {"name": "fault-S3-power-failure", "affected_cells": "S3/B3/C1", "affected_areas": "", "trigger_fm": False,
         "delay": 180, "duration": 0, "faultType": {"type": "commercialpowerfailure", "partial_outage_duration": 0,
                                                    "tx_power_reduction_percentage": 0}}
CELL_NAMES = ("S1/B3/C1", "S2/B3/C1", "S3/B3/C1", "S4/B3/C1")
PROBLEM_BELOW_PCT = 70


def wanted(name: str) -> dict:
    cfg = json.loads(BASELINE.read_text())
    cfg["System"]["duration"] = TARGETS[name]["duration"]
    cfg["Scenario_Generation"]["Summary"] = TARGETS[name]["summary"]
    return cfg


def check(cfg: dict) -> list:
    """What makes a config differ from the demo baseline (empty: it is the baseline)."""
    bad = []
    power, tilt = PLANNED["tx_power_dbm"], PLANNED["tilt_deg"]
    for p in cfg["Cells"]["Cell_Profiles"]:
        for c in p["cellsConfig"]:
            if (c.get("power"), c.get("tilt")) != (power, tilt):
                bad.append(f"profile {p['name']}/{c['cell_number']}: power {c.get('power')}, tilt {c.get('tilt')}")
    rows = list(csv.DictReader(io.StringIO(cfg["Cells"]["Import"]["CSV Import"].strip())))
    if tuple(r["Cell"] for r in rows) != CELL_NAMES:
        bad.append(f"cells {[r['Cell'] for r in rows]}, not {list(CELL_NAMES)}")
    for r in rows:  # a Power column (the agents' old scenario-config path) overrides the profile per cell
        if float(r.get("Tilt") or tilt) != tilt or float(r.get("Power") or power) != power:
            bad.append(f"{r['Cell']}: power {r.get('Power') or power}, tilt {r.get('Tilt')}")
    anomalies = cfg["Anomalies"]
    if anomalies.get("Power_Failures") != [FAULT]:
        bad.append(f"power failures {anomalies.get('Power_Failures')}, not S3 at 180 s only")
    for k in ("Radio_Anomalies", "Connectivity_Anomalies"):
        if anomalies.get(k):
            bad.append(f"{k}: {anomalies[k]}")
    return bad


def diff(a, b, path="") -> list:
    """The paths where two configs differ, for the operator."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b)) for d in diff(a.get(k), b.get(k), f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [path or "."]


def slot_free(link) -> bool:
    slot = link.run({"op": "slot"})
    if not slot["free"]:
        s = slot["status"]
        print(f"the twin's run slot is busy: {s.get('test_id')} · {s.get('status')} · sim_time {s.get('sim_time')}")
    return slot["free"]


def verify(link) -> bool:
    """A fast run (not paced to the wall clock) of the live scenario, four windows: the demo's shape, or not."""
    test_id = link.run({"op": "start", "scenario": VERIFY_SCENARIO})["test_id"]
    ok, first = True, None
    try:
        for m in range(1, 5):
            r = link.run({"op": "step", "test_id": test_id, "seconds": WINDOW_S, "to": m * WINDOW_S, "first": first})
            first, ratio = r["first"], r["window"]["network"]["coverage_ratio_rsrp_gt_-110"]
            pct = round(100 * ratio, 1) if ratio is not None else None
            expect_problem = m * WINDOW_S > FAULT["delay"]
            good = pct is not None and (pct < PROBLEM_BELOW_PCT) == expect_problem
            ok &= good
            print(f"  verify {test_id} min {m}: coverage {pct}% (want {'<' if expect_problem else '>='} "
                  f"{PROBLEM_BELOW_PCT}%) {'ok' if good else 'WRONG'}")
    finally:
        link.run({"op": "stop", "test_id": test_id})
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="compare only, write nothing")
    ap.add_argument("--verify", action="store_true", help=f"after the reset, a fast 4-minute run of {VERIFY_SCENARIO}")
    a = ap.parse_args()

    link = LocalLink()
    for name in TARGETS:  # the committed baseline itself must pass, or nothing is written
        if check(wanted(name)):
            raise SystemExit(f"{BASELINE.name} is not the demo baseline: {check(wanted(name))}")
    if not a.check and not slot_free(link):
        raise SystemExit("not resetting while a run is going; try again when it stops")

    drifted = []
    for name in TARGETS:
        want = wanted(name)
        have = link.run({"op": "scenario_get", "name": name})
        changed = diff(want, have)
        if not changed:
            print(f"{name}: baseline")
            continue
        print(f"{name}: differs at {', '.join(changed[:8])}{' …' if len(changed) > 8 else ''}")
        for b in check(have):
            print(f"  {b}")
        if a.check:
            drifted.append(name)
            continue
        link.run({"op": "scenario_put", "name": name, "config": copy.deepcopy(want)})
        back = diff(want, link.run({"op": "scenario_get", "name": name}))
        if back:
            raise SystemExit(f"{name}: saved, but reads back different at {', '.join(back[:8])}")
        print(f"{name}: reset to baseline")
    if drifted:
        raise SystemExit(f"drifted: {', '.join(drifted)} (run without --check to reset)")
    if a.verify:
        if not slot_free(link):
            raise SystemExit("not verifying while a run is going")
        if not verify(link):
            raise SystemExit("the baseline did not give the demo's shape: see the minutes above")
        print("verified: the problem opens from minute 4")


if __name__ == "__main__":
    main()
