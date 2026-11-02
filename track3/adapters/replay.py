"""The replay adapter: a frozen fault from scenarios/faults on mobile-env's layout, through rfmodel.

A live adapter reads the same KPIs from Dynatrace (the simulator's cell records). The agents and their tools don't
change when the adapter does.
"""

import json
from pathlib import Path

import rfmodel as m

FAULTS = Path(__file__).resolve().parent.parent / "scenarios" / "faults"


def _cell_id(cell_id) -> str:
    return str(cell_id or "").strip().upper()


class ReplayNetwork:
    source = "replay"

    def __init__(self, fault_name: str, run_id: str, taken_at: str):
        self.fault_path = FAULTS / f"{fault_name}.json"
        self.fault = json.loads(self.fault_path.read_text())
        self.layout = m.load_layout()
        inj = self.fault["injection"]
        self.config = m.with_change(m.default_config(), inj["cell_id"], inj["parameter"], inj["value"])
        self.alarms = self.fault.get("alarms", [])
        self.ledger = self.fault.get("ledger", [])
        self.snapshot = m.snapshot(self.layout, self.config, snapshot_id=f"SNAP-{run_id}-1", config_version=1, taken_at=taken_at)

    def cell(self, cell_id) -> dict:
        return m.cell(self.snapshot, _cell_id(cell_id)) or {"error": f"unknown cell {cell_id!r}; the cells are A to G"}

    def neighbors(self, cell_id):
        c = self.cell(cell_id)
        return c if "error" in c else [m.cell(self.snapshot, n["cell_id"]) for n in c["neighbors"]]

    def configuration(self, cell_id) -> dict:
        c = self.cell(cell_id)
        if "error" in c:
            return c
        desired = {k: self.config[c["cell_id"]][k] for k in m.PARAMETERS}
        observed = {k: c[k] for k in m.PARAMETERS}
        return {"cell_id": c["cell_id"], "desired": desired, "observed": observed, "in_sync": desired == observed,
                "classification": c["classification"], "maintenance_window": c["maintenance_window"]}

    def alarm_history(self, cell_id, window_minutes=60) -> list:
        cid = _cell_id(cell_id)
        rows = [{"minutes_ago": a["minutes_ago"], "kind": "alarm", "text": a["alarm"], "hardware_fault": a.get("hardware_fault")}
                for a in self.alarms if a["cell_id"] == cid and a["minutes_ago"] <= int(window_minutes or 60)]
        rows += [{"minutes_ago": e["minutes_ago"], "kind": "configuration change", "by": e["by"],
                  "text": f"{e['parameter']} {e['old_value']} -> {e['new_value']}"}
                 for e in self.ledger if e["cell_id"] == cid and e["minutes_ago"] <= int(window_minutes or 60)]
        return sorted(rows, key=lambda r: r["minutes_ago"])

    def simulate(self, cell_id, parameter: str, value) -> dict:
        return m.simulate_change(self.layout, self.config, _cell_id(cell_id), parameter, value)

    def simulate_delta(self, cell_id, parameter: str, delta) -> dict:
        cid = _cell_id(cell_id)
        if cid not in self.config:
            return {"error": f"unknown cell {cell_id!r}; the cells are A to G"}
        try:
            delta = int(round(float(delta)))
        except (TypeError, ValueError):
            return {"error": f"the change must be a number, got {delta!r}"}
        return self.simulate(cid, parameter, self.config[cid][parameter] + delta)

    def neighbor_impact(self, cell_id, parameter: str, value) -> dict:
        sim = self.simulate(cell_id, parameter, value)
        if "error" in sim:
            return sim
        watched = [c["cell_id"] for c in sim["predicted_cells"] if c["cell_id"] != sim["cell_id"]]
        return {"cell_id": sim["cell_id"], "change": f"{parameter} {sim['current_value']} -> {sim['proposed_value']}",
                "network_phones_dropped_delta": sim["deltas"]["network.phones_dropped"],
                "neighbors": [{"cell_id": c, "sinr_db_p10_delta": sim["deltas"].get(f"{c}.sinr_db_p10"),
                               "dropped_nearby_delta": sim["deltas"].get(f"{c}.dropped_nearby")} for c in watched],
                "simulation_ref": sim["simulation_ref"]}
