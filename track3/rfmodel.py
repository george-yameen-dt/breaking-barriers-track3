"""The swarm's RF model: declared mock physics, deterministic, pure Python.

It is the one source of physics for the snapshot the agents read, the what-if simulations they run and the
risk checks that recompute those simulations. It is deliberately simple; DATA.md declares it.

- Geometry: mobile-env's medium layout (scenarios/swarm-layout.json), 1 map unit = 20 m (a 4 x 6 km area).
- Path loss: COST-231 Hata, 1800 MHz, tower 50 m, phone 1.5 m, medium city.
- Antenna: omni in the horizontal plane; 3GPP TR 36.814 vertical pattern (10 degree beam, 20 dB floor), 15 dBi peak.
- Interference: every other switched-on cell at 70 % load. Noise: -97.5 dBm over 9 MHz.
- A phone is served by its strongest cell if RSRP >= -106 dBm and SINR >= -6 dB; otherwise it is dropped.
- Calibrated so that one dark cell (C) leaves about 2 of 15 phones without service, and switching it back on fixes it.
- A cell serves 5 phones at full load; its 9 MHz is shared equally between the phones it serves.
"""

import copy
import json
import math
from pathlib import Path

from contracts import canonical_json, sha256_of  # noqa: F401  (canonical_json is re-exported for the tools)

HERE = Path(__file__).parent
LAYOUT = HERE / "scenarios" / "swarm-layout.json"

METERS_PER_UNIT = 20
FREQ_MHZ = 1800
TOWER_M, PHONE_M = 50.0, 1.5
PEAK_GAIN_DBI = 15.0
BEAM_DEG, FLOOR_DB = 10.0, 20.0
RSRP_PER_RE_DB = 10 * math.log10(12 * 50)  # RSRP is the power of one of 600 resource elements (50 RBs)
NOISE_DBM = -97.5
LOAD = 0.7
RSRP_MIN_DBM, SINR_MIN_DB = -106.0, -6.0
PHONES_PER_CELL = 5
BANDWIDTH_HZ = 9e6

CELLS = "ABCDEFG"
PROTECTED = {"A": "emergency_services"}
PARAMETERS = ("enabled", "tx_power_dbm", "tilt_deg")


def load_layout(path: Path = LAYOUT) -> dict:
    return json.loads(Path(path).read_text())


def default_config() -> dict:
    return {c: {"enabled": 1, "tx_power_dbm": 43, "tilt_deg": 6, "classification": PROTECTED.get(c, "production"),
                "maintenance_window": 0} for c in CELLS}


def with_change(config: dict, cell_id: str, parameter: str, value: int) -> dict:
    changed = copy.deepcopy(config)
    changed[cell_id][parameter] = int(value)
    return changed


# ---- physics ---------------------------------------------------------------------------------
def path_loss_db(d_m: float) -> float:
    lf = math.log10(FREQ_MHZ)
    phone = (1.1 * lf - 0.7) * PHONE_M - (1.56 * lf - 0.8)
    return (46.3 + 33.9 * lf - 13.82 * math.log10(TOWER_M) - phone
            + (44.9 - 6.55 * math.log10(TOWER_M)) * math.log10(max(d_m, 20.0) / 1000))


def tilt_gain_db(d_m: float, tilt_deg: float) -> float:
    below_horizon = math.degrees(math.atan2(TOWER_M - PHONE_M, max(d_m, 1.0)))
    return -min(12 * ((below_horizon - tilt_deg) / BEAM_DEG) ** 2, FLOOR_DB)


def received_dbm(cell: dict, d_m: float) -> float:
    return cell["tx_power_dbm"] + PEAK_GAIN_DBI + tilt_gain_db(d_m, cell["tilt_deg"]) - path_loss_db(d_m)


def _mw(dbm: float) -> float:
    return 10 ** (dbm / 10)


def _pct(values: list, p: float):
    """Nearest-rank percentile, so every result is reproducible to the digit."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def _towers(layout: dict) -> dict:
    return {c: (x * METERS_PER_UNIT, y * METERS_PER_UNIT) for c, (x, y) in layout["stations"].items()}


def _neighbors(towers: dict, cell_id: str, n: int = 3) -> list:
    near = sorted((math.dist(towers[cell_id], towers[o]), o) for o in towers if o != cell_id)
    return [{"cell_id": o, "distance_m": round(d)} for d, o in near[:n]]


# ---- KPIs ------------------------------------------------------------------------------------
def network_kpis(layout: dict, config: dict) -> dict:
    """Per-cell and network KPIs over every phone sample in the layout."""
    towers = _towers(layout)
    on = [c for c in CELLS if config[c]["enabled"]]
    acc = {c: {"phones": 0, "prb": 0.0, "rates": [], "rsrp": [], "sinr": [], "dropped": 0, "throughput": 0.0} for c in CELLS}
    served_total = dropped_total = 0
    all_rates = []
    samples = layout["ue_samples"]
    for sample in samples:
        served = []
        for x, y in sample:
            pos = (x * METERS_PER_UNIT, y * METERS_PER_UNIT)
            rx = {c: received_dbm(config[c], math.dist(pos, towers[c])) for c in on}
            best = max(sorted(rx), key=rx.get) if rx else None
            if best:
                rsrp = rx[best] - RSRP_PER_RE_DB
                noise_and_interference = _mw(NOISE_DBM) + sum(LOAD * _mw(rx[c]) for c in on if c != best)
                sinr = rx[best] - 10 * math.log10(noise_and_interference)
                if rsrp >= RSRP_MIN_DBM and sinr >= SINR_MIN_DB:
                    served.append((best, rsrp, sinr))
                    continue
            nearest = min(CELLS, key=lambda c: (math.dist(pos, towers[c]), c))
            acc[nearest]["dropped"] += 1
            dropped_total += 1
        load = {c: sum(1 for s in served if s[0] == c) for c in CELLS}
        for c in CELLS:
            acc[c]["phones"] += load[c]
            acc[c]["prb"] += min(100.0, 100.0 * load[c] / PHONES_PER_CELL)
        for cell, rsrp, sinr in served:
            rate = BANDWIDTH_HZ / load[cell] * math.log2(1 + 10 ** (sinr / 10)) / 1e6
            acc[cell]["rates"].append(rate)
            acc[cell]["rsrp"].append(rsrp)
            acc[cell]["sinr"].append(sinr)
            acc[cell]["throughput"] += rate
            all_rates.append(rate)
        served_total += len(served)
    n = len(samples)
    cells = []
    for c in CELLS:
        a, cfg = acc[c], config[c]
        r1 = (lambda v: None if v is None else round(v, 1))
        cells.append({
            "cell_id": c, "enabled": int(cfg["enabled"]), "classification": cfg["classification"],
            "tx_power_dbm": int(cfg["tx_power_dbm"]), "tilt_deg": int(cfg["tilt_deg"]),
            "maintenance_window": int(cfg["maintenance_window"]),
            "active_users": round(a["phones"] / n, 1), "prb_utilization_pct": round(a["prb"] / n),
            "throughput_mbps": round(a["throughput"] / n, 1), "edge_throughput_mbps": r1(_pct(a["rates"], 10)),
            "rsrp_dbm_p50": r1(_pct(a["rsrp"], 50)), "sinr_db_p50": r1(_pct(a["sinr"], 50)), "sinr_db_p10": r1(_pct(a["sinr"], 10)),
            "dropped_nearby": round(a["dropped"] / n, 1), "neighbors": _neighbors(towers, c)})
    network = {"phones_active": len(samples[0]), "phones_served": round(served_total / n, 1),
               "phones_dropped": round(dropped_total / n, 1),
               "median_rate_mbps": round(_pct(all_rates, 50) or 0.0, 2)}
    return {"network": network, "cells": cells}


def snapshot(layout: dict, config: dict, snapshot_id: str = "", config_version: int = 0, taken_at: str = "") -> dict:
    """The deterministic KPI snapshot every agent gets. Its hash proves which numbers they were shown."""
    k = network_kpis(layout, config)
    snap = {"snapshot_id": snapshot_id, "emulator": "mobile-env medium layout + rfmodel (declared mock RF)",
            "samples": len(layout["ue_samples"]), "config_version": config_version, "taken_at": taken_at,
            "network": k["network"], "cells": k["cells"]}
    snap["snapshot_sha256"] = sha256_of(snap)
    return snap


def cell(snap: dict, cell_id: str) -> dict | None:
    return next((c for c in snap["cells"] if c["cell_id"] == cell_id), None)


def simulation_ref(config: dict, cell_id: str, parameter: str, value: int) -> str:
    """Short, recomputable id of one what-if (inputs only; the model is deterministic)."""
    return "SIM-" + sha256_of({"config": config, "change": [cell_id, parameter, int(value)]})[7:23]


def simulate_change(layout: dict, config: dict, cell_id: str, parameter: str, value) -> dict:
    """What-if: the same phones, one setting changed. Returns the predicted KPIs and the deltas."""
    if cell_id not in config:
        return {"error": f"unknown cell {cell_id!r}; cells are {', '.join(CELLS)}"}
    if parameter not in PARAMETERS:
        return {"error": f"unknown parameter {parameter!r}; use one of {', '.join(PARAMETERS)}"}
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        return {"error": f"value must be a number, got {value!r}"}
    before = network_kpis(layout, config)
    after = network_kpis(layout, with_change(config, cell_id, parameter, value))
    towers = _towers(layout)
    watch = [cell_id] + [n["cell_id"] for n in _neighbors(towers, cell_id)]
    b = {c["cell_id"]: c for c in before["cells"]}
    a = {c["cell_id"]: c for c in after["cells"]}

    def delta(x, y):
        return None if x is None or y is None else round(y - x, 1)

    deltas = {"network.phones_dropped": delta(before["network"]["phones_dropped"], after["network"]["phones_dropped"]),
              "network.median_rate_mbps": delta(before["network"]["median_rate_mbps"], after["network"]["median_rate_mbps"])}
    for c in watch:
        deltas[f"{c}.sinr_db_p10"] = delta(b[c]["sinr_db_p10"], a[c]["sinr_db_p10"])
        deltas[f"{c}.dropped_nearby"] = delta(b[c]["dropped_nearby"], a[c]["dropped_nearby"])
        deltas[f"{c}.prb_utilization_pct"] = delta(b[c]["prb_utilization_pct"], a[c]["prb_utilization_pct"])
    return {"cell_id": cell_id, "parameter": parameter, "current_value": int(config[cell_id][parameter]), "proposed_value": value,
            "predicted_network": after["network"],
            "predicted_cells": [{k: a[c][k] for k in ("cell_id", "enabled", "tx_power_dbm", "tilt_deg", "active_users",
                                                      "prb_utilization_pct", "sinr_db_p10", "dropped_nearby")} for c in watch],
            "deltas": deltas, "simulation_ref": simulation_ref(config, cell_id, parameter, value)}
