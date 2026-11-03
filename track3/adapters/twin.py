"""The twin adapter: the swarm on the Viavi AI RSG digital twin (team04's 4-cell scenario, cells S1 to S4).

Same interface as adapters/replay.py, so the agents, their tools and the checks don't change. Two sources:
- RecordedTwin: a saved twin run (scenarios/twin/proof-rsg-14.json, the saved proof run). Offline: nothing is sent.
- LiveTwin: the RSG itself, through adapters/twin_link.py (SSM on the one host the RSG answers).

Unlike the replay network, a change can be applied (apply), and its effect measured one window later (measure).

What-if (declared, deliberately simple): the twin has no "try it first" call, so a change is predicted from the
changed cell's own numbers. Its phones' RSRP is taken as normal (the twin's mean and p10); +N dB of power moves it up
N dB; uptilt moves it up 0.75 dB per degree (downtilt, down). A phone at or below -110 dBm has no service.
Neighbours are not predicted: the twin measures them one window after the change, and a change that made things worse
is undone.
"""

import json
import re
import math
from pathlib import Path

from adapters.criticality import criticality_of
from contracts import Refused
from contracts import sha256_of
from policy import critical_power_step

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios" / "twin"
CELLS = ("S1", "S2", "S3", "S4")
WINDOW_S = 60
REPORTS_PER_WINDOW = 60          # the RSG reports every phone once a second
RSRP_FLOOR_DBM = -110.0
TILT_DB_PER_DEG = 0.75
PLANNED = {"enabled": 1, "tx_power_dbm": 40, "tilt_deg": 6}
# The twin kit's safety filter (ran-rl-starter config/env_viavi.yaml), inside the swarm's own Cedar ranges.
POWER_RANGE, POWER_STEP = (30, 46), (-6, 8)
TILT_RANGE, TILT_STEP = (4, 10), 5
COMMANDS = {"tx_power_dbm": ("tx_power", "power"), "tilt_deg": ("antenna_tilt", "tilt")}

PROMPT_NOTE = """

THIS RUN IS ON THE VIAVI DIGITAL TWIN, not the 7-cell network described above. It has 4 cells, S1 to S4: use these
ids. Cells whose criticality is hospital, emergency or military serve critical sites: never lower their power or tilt them away, and raise their power by at most +{CRITICAL_STEP} dB
at a time (a larger step is refused, so do not propose it). The planned settings are tx_power_dbm 40 and tilt_deg 6. The twin cannot switch
a cell on or off remotely: a cell that lost power stays dark, so its neighbours must cover the gap. Allowed changes:
tx_power_dbm 30-46 dBm, at most +8 or -6 dB at a time; tilt_deg 4-10 degrees, at most 5 degrees at a time.
rsrp_dbm_p50 and sinr_db_p50 are means; rsrp_dbm_p10 is the weakest tenth of the phones. qos_score is the twin's
QoS Score, 0-100: the share of the speed its phones asked for that they got (100 = every phone gets its speed). The what-if tools predict
only the changed cell's phones; the twin measures the neighbours one window after the change."""
PROMPT_NOTE = PROMPT_NOTE.replace("{CRITICAL_STEP}", str(critical_power_step()))  # the limit the policy enforces


def short(name) -> str:
    """'S4/B3/C1' or 's4' -> 'S4'."""
    return str(name or "").split("/")[0].strip().upper()


def discover(window: dict) -> dict:
    """short cell id -> the Viavi cell name, from the cells the twin reports, in natural order (S2 before S10).
    A window with no cells falls back to the four planned cells."""
    found = {}
    for name in window.get("cells", {}):
        found.setdefault(short(name), str(name))
    order = lambda kv: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", kv[0])]  # noqa: E731
    return dict(sorted(found.items(), key=order)) or {c: full(c) for c in CELLS}


def full(cell_id) -> str:
    return f"{short(cell_id)}/B3/C1"


def _r(v, n=1):
    return None if v is None else round(float(v), n)


def uncovered_fraction(mean, p10, shift: float = 0.0):
    """Share of a cell's phones at or below the RSRP floor, with RSRP taken as normal (mean, p10), moved by shift dB."""
    if mean is None or p10 is None:
        return None
    sigma = max(1.0, (mean - p10) / 1.2816)
    return 0.5 * (1 + math.erf((RSRP_FLOOR_DBM - (mean + shift)) / sigma / math.sqrt(2)))


def snapshot(window: dict, config: dict, phones_active: float, snapshot_id: str = "", config_version: int = 0,
             taken_at: str = "", scenario: str = "") -> dict:
    """The KPI snapshot the agents read, from one twin window (the same fields as rfmodel.snapshot)."""
    by = {short(k): v for k, v in window["cells"].items()}
    cells = []
    names = discover(window)
    for c in config:
        k, cfg = by.get(c, {}), config[c]
        ues = float(k.get("ues") or 0.0)
        on = (k.get("power") if k.get("power") is not None else -999) > -900 and ues > 0
        frac = uncovered_fraction(k.get("rsrp_mean"), k.get("rsrp_p10"))
        cells.append({
            "cell_id": c, "enabled": int(on), "classification": cfg["classification"], "criticality": criticality_of(c),
            "cell_full": names.get(c, full(c)), "pci": k.get("pci"), "nrcgi": k.get("nrcgi"), "frequency": k.get("frequency"),
            "tx_power_dbm": int(cfg["tx_power_dbm"]), "tilt_deg": int(cfg["tilt_deg"]),
            "maintenance_window": int(cfg["maintenance_window"]),
            "active_users": round(ues, 1),
            # RRU.PrbTotDl is the load in %; older windows (the saved recording) only have the PRB count
            "prb_utilization_pct": round(float(k["prb_pct"] if k.get("prb_pct") is not None else k.get("prb_dl") or 0)),
            "qos_score": _r(k.get("qos")),
            "throughput_mbps": _r(k.get("thp_dl")), "edge_throughput_mbps": None,
            "rsrp_dbm_p50": _r(k.get("rsrp_mean")), "rsrp_dbm_p10": _r(k.get("rsrp_p10")),
            "sinr_db_p50": _r(k.get("sinr_mean")), "sinr_db_p10": _r(k.get("sinr_p10")),
            "rsrq_db_p50": _r(k.get("rsrq_mean")),
            "dropped_nearby": round(ues * frac, 1) if frac is not None else 0.0,
            "neighbors": [{"cell_id": o, "distance_m": None} for o in config if o != c]})
    n = window["network"]
    served = float(n.get("coverage_ratio_rsrp_gt_-110") or 0) * float(n.get("ue_samples") or 0) / REPORTS_PER_WINDOW
    snap = {"snapshot_id": snapshot_id,
            "emulator": f"Viavi AI RSG digital twin ({scenario}), window {window['t'][0]}-{window['t'][1]} s",
            "samples": REPORTS_PER_WINDOW, "config_version": config_version, "taken_at": taken_at,
            "window_s": list(window["t"]),
            "network": {"phones_active": phones_active, "phones_served": round(served, 1),
                        "phones_dropped": round(max(0.0, phones_active - served), 1),
                        "median_rate_mbps": _r(n.get("ue_thp_dl_mean"), 2), "qos_score": _r(n.get("ue_qos_mean")),
                        "coverage_pct": _r(100 * n["coverage_ratio_rsrp_gt_-110"])
                        if n.get("coverage_ratio_rsrp_gt_-110") is not None else None},
            "cells": cells}
    snap["snapshot_sha256"] = sha256_of(snap)
    return snap


def cell(snap: dict, cell_id) -> dict | None:
    return next((c for c in snap["cells"] if c["cell_id"] == short(cell_id)), None)


def alarms_from(events: list, now_s: float, cells=CELLS) -> list:
    """The twin's own events (sim time t) as alarms: a cell shut down, a raised anomaly, phones losing their radio link."""
    alarms, lost = [], {}
    for e in events:
        kind, c, t = e["event_type"].replace("\\ ", " "), short(e.get("cell")), float(e["t"])
        if t > now_s or c not in cells:
            continue
        ago = max(0, round((now_s - t) / 60))
        if kind == "turn_off":
            alarms.append({"cell_id": c, "minutes_ago": ago, "hardware_fault": True,
                           "alarm": f"cell unavailable: {e.get('description', '')}"})
        elif kind == "Anomaly State Change" and str(e.get("description", "")).startswith("raise"):
            power = "power" in str(e.get("description", "")).lower()
            alarms.append({"cell_id": c, "minutes_ago": ago, "hardware_fault": power,
                           "alarm": ("site power failure" if power else "radio degradation") + f" ({e['description']})"})
        elif kind == "Radio Link Failure":
            lost.setdefault(c, []).append(ago)
    for c, agos in lost.items():
        alarms.append({"cell_id": c, "minutes_ago": min(agos), "hardware_fault": False,
                       "alarm": f"{len(agos)} phone(s) lost their radio link (RSRP below -140 dBm)"})
    return alarms


# ---- the two sources -----------------------------------------------------------------------------
class RecordedTwin:
    """A saved twin run: its windows, its events and the commands it accepted. Nothing is sent anywhere, so the window
    after a change is the saved one: it shows the saved run's own change, not necessarily this run's."""
    source = "recorded"

    def __init__(self, path: Path, first_timestamp: float):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text())
        self.first = float(first_timestamp)
        self.windows = {tuple(w["t"]): w for w in self.data["windows"]}
        self.t = max(t1 for _, t1 in self.windows)
        self.sent = []

    def advance_to(self, t: int) -> None:
        if (t - WINDOW_S, t) not in self.windows:
            raise RuntimeError(f"the recording has no window ending at {t} s")

    def window(self, t0: int, t1: int) -> dict:
        return self.windows[(t0, t1)]

    def events(self, until_s: float) -> list:
        return [{**e, "t": int(e["time"]) / 1000 - self.first} for e in self.data["events"]
                if int(e["time"]) / 1000 - self.first <= until_s]

    def prior_commands(self, until_s: float) -> list:
        """The saved run's accepted changes in effect before until_s, as (sim time, cell, parameter, value)."""
        out = []
        for c in self.data["commands"]:
            if c["sim_time"] < until_s and (c.get("reply") or {}).get("accepted"):
                parameter = "tx_power_dbm" if c["command_type"] == "tx_power" else "tilt_deg"
                value = c["params"]["power"] if parameter == "tx_power_dbm" else c["params"]["tilt"]
                out.append((c["sim_time"], short(c["params"]["cell"]), parameter, int(value)))
        return out

    def command(self, t: float, command_type: str, params: dict, reason: str) -> dict:
        self.sent.append({"t": t, "command_type": command_type, "params": params, "reason": reason})
        return {"accepted": True, "command_type": command_type, "recorded": True,
                "note": "recorded twin: the command was not sent anywhere"}

    def close(self) -> None:
        pass


class LiveTwin:
    """The RSG itself: one run of the scenario, advanced 60 s at a time, always stopped at the end."""
    source = "live"

    def __init__(self, link, scenario: str):
        self.link, self.scenario = link, scenario
        self.test_id = link.run({"op": "start", "scenario": scenario})["test_id"]
        self.first, self.t, self.windows = None, 0, {}

    def advance_to(self, t: int) -> None:
        while self.t < t:
            r = self.link.run({"op": "step", "test_id": self.test_id, "seconds": WINDOW_S, "to": self.t + WINDOW_S,
                               "first": self.first})
            self.first = r["first"]
            self.windows[(self.t, self.t + WINDOW_S)] = r["window"]
            self.t += WINDOW_S

    def window(self, t0: int, t1: int) -> dict:
        return self.windows[(t0, t1)]

    def events(self, until_s: float) -> list:
        rows = self.link.run({"op": "events", "test_id": self.test_id, "first": self.first, "until": until_s})
        return [{**e, "t": int(e["time"]) / 1000 - self.first} for e in rows]

    def prior_commands(self, until_s: float) -> list:
        return []

    def command(self, t: float, command_type: str, params: dict, reason: str) -> dict:
        return self.link.run({"op": "command", "test_id": self.test_id, "command_type": command_type,
                              "params": params, "reason": reason})

    def close(self) -> None:
        self.link.run({"op": "stop", "test_id": self.test_id})


class AttachedTwin:
    """A run the operator's poller (run8.py) is driving. The agents read it and send commands; they never start,
    advance or stop it. advance_to waits for the run's own clock; the poller moves it."""
    source = "live"
    history = False  # the poller already sends every window to Dynatrace

    def __init__(self, link):
        self.link = link
        s = link.run({"op": "attach"})
        self.test_id, self.first, self.now_s = s["test_id"], float(s["first_timestamp"]), int(s["sim_time"])
        self.windows, self.event_rows, self.applied_s = {}, [], None

    def window_after(self, now: int) -> tuple:
        """The window that shows a change: the run's clock moves on while the command is sent, so a window already
        recorded may predate it. After a command it is the first full window from the next boundary on."""
        if self.applied_s is None:
            return now, now + WINDOW_S
        t0 = max(now, int(math.ceil(self.applied_s / WINDOW_S)) * WINDOW_S)
        return t0, t0 + WINDOW_S

    def advance_to(self, t: int) -> None:
        if t > self.now_s:
            self.link.run({"op": "wait_for", "test_id": self.test_id, "to": t, "timeout": 300})
            self.now_s = t

    def window(self, t0: int, t1: int) -> dict:
        if (t0, t1) not in self.windows:
            self.windows[(t0, t1)] = self.link.run({"op": "window", "test_id": self.test_id, "first": self.first, "t0": t0, "t1": t1})
        return self.windows[(t0, t1)]

    def events(self, until_s: float) -> list:
        rows = self.link.run({"op": "events", "test_id": self.test_id, "first": self.first, "until": until_s})
        self.event_rows = [{**e, "t": int(e["time"]) / 1000 - self.first} for e in rows]
        return self.event_rows

    def prior_commands(self, until_s: float) -> list:
        return []

    def command(self, t: float, command_type: str, params: dict, reason: str) -> dict:
        reply = self.link.run({"op": "command", "test_id": self.test_id, "command_type": command_type,
                               "params": params, "reason": reason})
        self.applied_s = float(self.link.run({"op": "attach"})["sim_time"])  # when, on the run's own clock, it was sent
        return reply

    def state(self) -> dict:
        """What this run read, as JSON: the other agents' runtimes rebuild the same snapshot from it (FrozenTwin)."""
        return {"first": self.first, "now_s": self.now_s, "events": self.event_rows,
                "windows": [[t0, t1, w] for (t0, t1), w in sorted(self.windows.items())]}

    def close(self) -> None:
        pass  # the run is the operator's


class FrozenTwin:
    """A read-only copy of what an AttachedTwin read: no clock, no commands. For the specialists and the critic."""
    source = "live"
    history = False

    def __init__(self, state: dict):
        self.first, self.now_s, self.rows = state["first"], state["now_s"], state["events"]
        self.windows = {(w[0], w[1]): w[2] for w in state["windows"]}

    def advance_to(self, t: int) -> None:
        pass

    def window(self, t0: int, t1: int) -> dict:
        return self.windows[(t0, t1)]

    def events(self, until_s: float) -> list:
        return self.rows

    def prior_commands(self, until_s: float) -> list:
        return []

    def command(self, t: float, command_type: str, params: dict, reason: str) -> dict:
        return {"accepted": False, "error": "this runtime holds a read-only copy of the twin"}

    def close(self) -> None:
        pass


# ---- the network the agents see -------------------------------------------------------------------
class TwinNetwork:
    """ReplayNetwork's interface on the twin, plus apply and measure."""
    can_apply = True
    prompt_note = PROMPT_NOTE

    def __init__(self, twin, scenario_name: str, run_id: str, taken_at: str, dt=None, cp=None, incident_window=None):
        self.twin, self.run_id, self.taken_at = twin, run_id, taken_at
        self.cp = cp  # the checkpoint: every read of the Viavi MCP and every command is a Cedar decision when it is set
        self.dt = dt  # adapters/twin_dt.DynatraceLogs: every snapshot and change also goes to Dynatrace (or a dry-run file)
        self.source = f"viavi-twin ({twin.source})"
        self.fault_path = SCENARIOS / f"{scenario_name}.json"
        self.fault = json.loads(self.fault_path.read_text())
        self.layout = None
        t0, t1 = incident_window or self.fault["twin"]["incident_window"]
        twin.advance_to(WINDOW_S)
        first = twin.window(0, WINDOW_S)
        self.names = discover(first)  # the cells come from the data, not from a fixed list
        self.prompt_note = PROMPT_NOTE.replace("It has 4 cells, S1 to S4: use these\nids.", f"It has {len(self.names)} cells, "
                                                f"{', '.join(self.names)}: use these ids.")
        self.phones_active = round(float(first["network"]["ue_samples"]) / REPORTS_PER_WINDOW, 1)
        twin.advance_to(t1)
        self.now, self.version = t1, 0
        self.config = {c: {**PLANNED, "classification": "production", "maintenance_window": 0} for c in self.names}
        self.ledger = []
        for t, c, parameter, value in twin.prior_commands(t1):  # changes made before this run: not this run's agents
            self.ledger.append({"cell_id": c, "parameter": parameter, "old_value": self.config[c][parameter],
                                "new_value": value, "by": "operator", "at_s": t})
            self.config[c][parameter] = value
        if dt and getattr(twin, "history", True):  # the minutes before the incident, dated back from now, so Dynatrace shows the network before it broke
            for a in range(0, t0, WINDOW_S):
                dt.snapshot(snapshot(twin.window(a, a + WINDOW_S), self.config, self.phones_active,
                                     scenario=self.fault["twin"]["scenario"]), twin.source, seconds_ago=t1 - a - WINDOW_S)
        self._take(t0, t1)

    def _gate(self, action: str, tool: str, context: dict | None = None) -> None:
        if self.cp is not None:
            self.cp.gate_tool('Agent::"orchestrator"', action, tool, context or {})

    def _take(self, t0: int, t1: int) -> dict:
        self._gate("viavi_read", "viavi:simulation_query")
        self.version += 1
        w = self.twin.window(t0, t1)
        self.kpis = {short(k): v for k, v in w["cells"].items()}
        self.snapshot = snapshot(w, self.config, self.phones_active, snapshot_id=f"SNAP-{self.run_id}-{self.version}",
                                 config_version=self.version, taken_at=self.taken_at,
                                 scenario=self.fault["twin"]["scenario"])
        self.alarms = alarms_from(self.twin.events(t1), t1, self.config)
        for e in self.ledger:
            e["minutes_ago"] = max(0, round((self.now - e["at_s"]) / 60))
        if self.dt:
            self.dt.snapshot(self.snapshot, self.twin.source)
        return self.snapshot

    # -- reading, as ReplayNetwork --
    def cell(self, cell_id) -> dict:
        return cell(self.snapshot, cell_id) or {"error": f"unknown cell {cell_id!r}; the twin's cells are {', '.join(self.config)}"}

    def neighbors(self, cell_id):
        c = self.cell(cell_id)
        return c if "error" in c else [cell(self.snapshot, n["cell_id"]) for n in c["neighbors"]]

    def configuration(self, cell_id) -> dict:
        c = self.cell(cell_id)
        if "error" in c:
            return c
        desired = {k: self.config[c["cell_id"]][k] for k in ("enabled", "tx_power_dbm", "tilt_deg")}
        power = (self.kpis.get(c["cell_id"]) or {}).get("power")
        observed = {"enabled": c["enabled"], "tx_power_dbm": int(power) if power is not None and power > -900 else None,
                    "tilt_deg": c["tilt_deg"]}
        return {"cell_id": c["cell_id"], "desired": desired, "observed": observed, "in_sync": desired == observed,
                "classification": c["classification"], "maintenance_window": c["maintenance_window"],
                "note": "observed tilt is the last value the twin accepted; the twin does not report tilt"}

    def alarm_history(self, cell_id, window_minutes=60) -> list:
        cid, w = short(cell_id), int(window_minutes or 60)
        rows = [{"minutes_ago": a["minutes_ago"], "kind": "alarm", "text": a["alarm"], "hardware_fault": a["hardware_fault"]}
                for a in self.alarms if a["cell_id"] == cid and a["minutes_ago"] <= w]
        rows += [{"minutes_ago": e["minutes_ago"], "kind": "configuration change", "by": e["by"],
                  "text": f"{e['parameter']} {e['old_value']} -> {e['new_value']}"}
                 for e in self.ledger if e["cell_id"] == cid and e["minutes_ago"] <= w]
        return sorted(rows, key=lambda r: r["minutes_ago"])

    # -- what-if --
    def simulate(self, cell_id, parameter: str, value) -> dict:
        cid = short(cell_id)
        if cid not in self.config:
            return {"error": f"unknown cell {cell_id!r}; the twin's cells are {', '.join(self.config)}"}
        if parameter == "enabled":
            return {"error": "the twin cannot switch a cell on or off remotely; a dark cell's neighbours must cover the gap"}
        if parameter not in COMMANDS:
            return {"error": f"unknown parameter {parameter!r}; use tx_power_dbm or tilt_deg"}
        try:
            value = int(round(float(value)))
        except (TypeError, ValueError):
            return {"error": f"value must be a number, got {value!r}"}
        c, current = self.cell(cid), int(self.config[cid][parameter])
        if not c["enabled"]:
            return {"error": f"{cid} is not transmitting, so its settings change nothing; change a neighbour instead"}
        step = value - current
        if parameter == "tx_power_dbm":
            if not (POWER_RANGE[0] <= value <= POWER_RANGE[1] and POWER_STEP[0] <= step <= POWER_STEP[1]):
                return {"error": f"outside the twin's safe range: tx_power_dbm {POWER_RANGE[0]}-{POWER_RANGE[1]}, "
                                 f"at most +{POWER_STEP[1]} or {POWER_STEP[0]} dB at a time"}
            shift = float(step)
        else:
            if not (TILT_RANGE[0] <= value <= TILT_RANGE[1] and abs(step) <= TILT_STEP):
                return {"error": f"outside the twin's safe range: tilt_deg {TILT_RANGE[0]}-{TILT_RANGE[1]}, "
                                 f"at most {TILT_STEP} degrees at a time"}
            shift = -TILT_DB_PER_DEG * step
        k = self.kpis[cid]
        after = round(c["active_users"] * uncovered_fraction(k["rsrp_mean"], k["rsrp_p10"], shift), 1)
        d = round(after - c["dropped_nearby"], 1)
        net = self.snapshot["network"]
        predicted_network = {**net, "phones_served": round(net["phones_served"] - d, 1),
                             "phones_dropped": round(max(0.0, net["phones_dropped"] + d), 1)}
        watch = [cid] + [n["cell_id"] for n in c["neighbors"]]
        predicted_cells = []
        for w in watch:
            pc = {k2: cell(self.snapshot, w)[k2] for k2 in ("cell_id", "enabled", "tx_power_dbm", "tilt_deg", "active_users",
                                                           "prb_utilization_pct", "sinr_db_p10", "dropped_nearby")}
            if w == cid:
                pc.update({parameter: value, "dropped_nearby": after})
            predicted_cells.append(pc)
        deltas = {"network.phones_dropped": round(predicted_network["phones_dropped"] - net["phones_dropped"], 1),
                  "network.median_rate_mbps": None}
        for w in watch:  # neighbours: not predicted (None), measured on the twin after the change
            deltas[f"{w}.sinr_db_p10"] = None
            deltas[f"{w}.dropped_nearby"] = d if w == cid else None
            deltas[f"{w}.prb_utilization_pct"] = None
        return {"cell_id": cid, "parameter": parameter, "current_value": current, "proposed_value": value,
                "predicted_network": predicted_network, "predicted_cells": predicted_cells, "deltas": deltas,
                "model": "twin what-if: the changed cell's phones only; neighbours are measured after the change",
                "simulation_ref": "SIM-" + sha256_of({"snapshot": self.snapshot["snapshot_sha256"],
                                                      "change": [cid, parameter, value]})[7:23]}

    def simulate_delta(self, cell_id, parameter: str, delta) -> dict:
        cid = short(cell_id)
        if cid not in self.config:
            return {"error": f"unknown cell {cell_id!r}; the twin's cells are {', '.join(self.config)}"}
        try:
            delta = int(round(float(delta)))
        except (TypeError, ValueError):
            return {"error": f"the change must be a number, got {delta!r}"}
        return self.simulate(cid, parameter, self.config[cid].get(parameter, 0) + delta)

    def neighbor_impact(self, cell_id, parameter: str, value) -> dict:
        sim = self.simulate(cell_id, parameter, value)
        if "error" in sim:
            return sim
        return {"cell_id": sim["cell_id"], "change": f"{parameter} {sim['current_value']} -> {sim['proposed_value']}",
                "network_phones_dropped_delta": sim["deltas"]["network.phones_dropped"],
                "neighbors": [{"cell_id": c["cell_id"], "sinr_db_p10_delta": None, "dropped_nearby_delta": None}
                              for c in sim["predicted_cells"] if c["cell_id"] != sim["cell_id"]],
                "note": "the twin what-if does not predict neighbours; the twin measures them one window after the change",
                "simulation_ref": sim["simulation_ref"]}

    # -- hands --
    def apply(self, cell_id, parameter: str, value: int, reason: str, by: str = "agent") -> dict:
        """Send one change to the twin (the next window runs with it). Only the orchestrator calls this, after Cedar."""
        cid = short(cell_id)
        command_type, key = COMMANDS[parameter]
        try:
            self._gate("viavi_command", "viavi:simulation_command",
                       {"command_type": command_type, "delay_s": 0,
                        "reason_has_trace_id": bool(self.cp is not None and self.cp.trace_id in reason)})
        except Refused as e:
            return {"accepted": False, "error": f"refused by Cedar: {e}"}
        reply = self.twin.command(self.now, command_type, {"cell": self.names.get(cid, full(cid)), key: int(value)}, reason)
        if self.dt:
            self.dt.change(cell=cid, parameter=parameter, old_value=self.config[cid][parameter], new_value=int(value),
                           accepted=bool(isinstance(reply, dict) and reply.get("accepted")), reason=reason)
        if isinstance(reply, dict) and reply.get("accepted"):
            self.ledger.append({"cell_id": cid, "parameter": parameter, "old_value": self.config[cid][parameter],
                                "new_value": int(value), "by": by, "at_s": self.now, "minutes_ago": 0})
            self.config[cid][parameter] = int(value)
        return reply if isinstance(reply, dict) else {"reply": reply}

    def measure(self) -> dict:
        """Run the twin one more window and take a new snapshot."""
        t0, t1 = self.twin.window_after(self.now) if hasattr(self.twin, "window_after") else (self.now, self.now + WINDOW_S)
        self.twin.advance_to(t1)
        self.now = t1
        return self._take(t0, t1)

    def close(self) -> None:
        if self.dt:
            self.dt.close()
        self.twin.close()


def open_twin(mode: str, scenario_name: str, run_id: str, taken_at: str, link=None, dt=None, cp=None) -> TwinNetwork:
    """mode 'recorded' (the saved run, offline) or 'live' (the RSG through twin_link)."""
    spec = json.loads((SCENARIOS / f"{scenario_name}.json").read_text())["twin"]
    window = None
    if mode == "recorded":
        twin = RecordedTwin(SCENARIOS / spec["recording"], spec["recording_first_timestamp"])
    elif mode == "attach":  # the operator's run: the newest full window is the incident window
        from adapters.twin_link import TwinLink
        twin = AttachedTwin(link or TwinLink())
        t1 = int(twin.now_s // WINDOW_S) * WINDOW_S
        window = (t1 - WINDOW_S, t1)
    else:
        from adapters.twin_link import TwinLink
        twin = LiveTwin(link or TwinLink(), spec["scenario"])
    try:
        return TwinNetwork(twin, scenario_name, run_id, taken_at, dt=dt, cp=cp, incident_window=window)
    except Exception:
        if dt:
            dt.close()
        twin.close()
        raise


def open_frozen(state: dict, scenario_name: str, run_id: str, taken_at: str, cp=None) -> TwinNetwork:
    """The same network as the orchestrator's, rebuilt from the state its AttachedTwin read. Commands are refused."""
    window = tuple(state["windows"][-1][:2]) if state["windows"] else None
    t1 = int(state["now_s"] // WINDOW_S) * WINDOW_S
    return TwinNetwork(FrozenTwin(state), scenario_name, run_id, taken_at, cp=cp, incident_window=(t1 - WINDOW_S, t1))
