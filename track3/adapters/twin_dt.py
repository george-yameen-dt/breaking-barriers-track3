"""The twin's numbers and the agents' changes as Dynatrace logs (service.name viavi-twin), for the problem card.

Off unless a run asks for it (swarm.py --dt):
- dry-run: the records go to a local JSON-lines file; nothing is sent.
- send: they go to the sprint tenant's log ingest (yqs73977) with DT_PLATFORM_TOKEN from the environment, else from
  ~/.bindplane/.env. The token needs the storage:logs:write scope.

With more than ALERT_PHONES phones without service, the records carry twin.alert: "tower_down" on each dark tower, or
"phones_without_service" on the network record when no tower is dark. The OpenPipeline pipeline
(scenarios/twin/dynatrace/pipeline.json) draws the network and its towers in Smartscape and raises the Davis event on
the alerting tower, so the problem has a root cause entity. The twin is paused while the agents work, so a heartbeat
re-sends the newest records every 30 s: Dynatrace keeps seeing the outage until a change fixes it.
"""

import json
import os
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

INGEST = "https://yqs73977.sprint.apps.dynatracelabs.com/platform/classic/environment-api/v2/logs/ingest"
ENV_FILE = Path.home() / ".bindplane" / ".env"
SERVICE = "viavi-twin"
NETWORK, NETWORK_NAME = "viavi-twin-team04", "Viavi twin (team04)"
ALERT_PHONES = 13.0  # in Stan's saved run: 9-10 before the S3 power failure, 16 after it, 12 after the fix
HEARTBEAT_S = 30


def _token() -> str:
    """DT_PLATFORM_TOKEN from the environment (the EC2's viavi-run sources it), else from ENV_FILE (the laptop)."""
    if os.environ.get("DT_PLATFORM_TOKEN", "").strip():
        return os.environ["DT_PLATFORM_TOKEN"].strip().strip("\"'")
    for line in (ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []):
        if line.startswith("DT_PLATFORM_TOKEN="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit(f"DT_PLATFORM_TOKEN is not in the environment or {ENV_FILE}")


def snapshot_records(snap: dict, run_id: str, trace_id: str, source: str) -> list:
    """One network record and one record per cell (a tower in Smartscape), with the alert flags."""
    n = snap["network"]
    worst = max(snap["cells"], key=lambda c: c["dropped_nearby"])
    dark = [c["cell_id"] for c in snap["cells"] if not c["enabled"]]
    alert = n["phones_dropped"] > ALERT_PHONES
    base = {"service.name": SERVICE, "log.source": "breaking-barriers/track3", "trace_id": trace_id,
            "twin.run_id": run_id, "twin.source": source, "twin.window_s": "-".join(str(t) for t in snap["window_s"]),
            "twin.snapshot_sha256": snap["snapshot_sha256"], "twin.network": NETWORK, "twin.network_name": NETWORK_NAME}
    records = [{**base, "twin.kind": "network", "loglevel": "WARN" if dark else "INFO",
                "content": f"{n['phones_dropped']} of {n['phones_active']} phones without service; dark cells: "
                           f"{', '.join(dark) or 'none'}; most of them near {worst['cell_id']}",
                "twin.phones_without_service": n["phones_dropped"], "twin.phones_active": n["phones_active"],
                "twin.phones_served": n["phones_served"], "twin.median_rate_mbps": n["median_rate_mbps"],
                "twin.qos_score": n.get("qos_score"), "twin.coverage_pct": n.get("coverage_pct"),
                "twin.worst_cell": worst["cell_id"], "twin.dark_cells": ",".join(dark) or "none"}]
    if alert and not dark:
        records[0]["twin.alert"] = "phones_without_service"
    for c in snap["cells"]:
        rec = {**base, "twin.kind": "cell", "loglevel": "INFO" if c["enabled"] else "WARN", "twin.cell": c["cell_id"],
               "twin.cell_name": f"Tower {c['cell_id']}", "twin.state": "on" if c["enabled"] else "DARK",
               "content": f"cell {c['cell_id']}: {'on' if c['enabled'] else 'DARK'}, {c['tx_power_dbm']} dBm, "
                          f"tilt {c['tilt_deg']}, {c['active_users']} phones, {c['dropped_nearby']} without service, "
                          f"QoS {c.get('qos_score')}, load {c.get('prb_utilization_pct')}%",
               "twin.enabled": c["enabled"], "twin.tx_power_dbm": c["tx_power_dbm"], "twin.tilt_deg": c["tilt_deg"],
               "twin.active_users": c["active_users"], "twin.dropped_nearby": c["dropped_nearby"],
               "twin.rsrp_dbm_p10": c["rsrp_dbm_p10"], "twin.sinr_db_p10": c["sinr_db_p10"],
               "twin.qos_score": c.get("qos_score"), "twin.load_pct": c.get("prb_utilization_pct"),
               "twin.throughput_mbps": c.get("throughput_mbps"), "twin.rsrq_db": c.get("rsrq_db_p50"),
               # the other towers (the scenario gives every cell all others as neighbours): Smartscape draws the mesh
               **{f"twin.neighbor_{i}": o["cell_id"] for i, o in enumerate(
                   (o for o in snap["cells"] if o["cell_id"] != c["cell_id"]), 1)}}
        if alert and not c["enabled"]:
            others = ", ".join(f"{o['cell_id']} QoS {o.get('qos_score')} load {o.get('prb_utilization_pct')}% "
                               f"({o['active_users']} phones)" for o in snap["cells"] if o["enabled"])
            rec.update({"twin.alert": "tower_down", "twin.network_phones_without_service": n["phones_dropped"],
                        "twin.network_phones_active": n["phones_active"], "twin.network_worst_cell": worst["cell_id"],
                        "twin.network_qos_score": n.get("qos_score") if n.get("qos_score") is not None else "n/a",
                        "twin.towers_still_on": others,
                        "twin.what_next": f"{c['cell_id']} cannot be switched back on remotely: send a crew to restore "
                                          f"power, and let the neighbours cover the gap (more power or less tilt on "
                                          f"{worst['cell_id']}, watching its load). The agents propose the change; "
                                          f"a person approves it."})
        records.append(rec)
    return [{k: v for k, v in r.items() if v is not None} for r in records]


def change_record(run_id: str, trace_id: str, **fields) -> dict:
    """A change an agent sent to the twin (or its undo), as the twin's own reason string carries it."""
    return {"service.name": SERVICE, "log.source": "breaking-barriers/track3", "trace_id": trace_id, "twin.run_id": run_id,
            "twin.network": NETWORK, "twin.network_name": NETWORK_NAME, "twin.cell_name": f"Tower {fields.get('cell')}",
            "twin.kind": "change", "loglevel": "INFO", "content": "change: " + json.dumps(fields, default=str)[:800],
            **{f"twin.{k}": v for k, v in fields.items() if isinstance(v, (str, int, float))}}


class DynatraceLogs:
    """Ships the records (or writes them to a file in dry-run), with the heartbeat. Never stops the run.
    heartbeat=False for a run that sends its own record every minute (run8.py): one point per minute, no re-sends."""

    def __init__(self, mode: str, run_id: str, trace_id: str, dry_file: Path | None = None, heartbeat: bool = True):
        if mode not in ("dry-run", "send"):
            raise ValueError(f"mode must be dry-run or send, got {mode!r}")
        self.mode, self.run_id, self.trace_id, self.dry_file = mode, run_id, trace_id, dry_file
        self.token = _token() if mode == "send" else ""
        self.sent, self.errors, self.last_error, self.latest = 0, 0, "", None
        self._stop, self._lock = threading.Event(), threading.Lock()
        if heartbeat:
            threading.Thread(target=self._heartbeat, daemon=True).start()

    def snapshot(self, snap: dict, source: str, seconds_ago: float = 0, extra: dict | None = None) -> None:
        """seconds_ago dates a window from before the incident back; only the newest window feeds the heartbeat.
        extra: fields added to every record (the 8-minute test's phase and minute)."""
        records = [{**r, **(extra or {})} for r in snapshot_records(snap, self.run_id, self.trace_id, source)]
        if not seconds_ago:
            self.latest = records
        self._ship(records, seconds_ago)

    def change(self, **fields) -> None:
        self._ship([change_record(self.run_id, self.trace_id, **fields)])

    def _ship(self, records: list, seconds_ago: float = 0) -> None:
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="milliseconds")
        records = [{"timestamp": stamp, **r} for r in records]
        with self._lock:
            if self.mode == "dry-run":
                self.dry_file.parent.mkdir(parents=True, exist_ok=True)
                with self.dry_file.open("a") as f:
                    f.writelines(json.dumps(r) + "\n" for r in records)
                self.sent += len(records)
                return
            req = urllib.request.Request(INGEST, data=json.dumps(records).encode(), headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=utf-8"})
            try:
                urllib.request.urlopen(req, timeout=10).read()
                self.sent += len(records)
            except Exception as e:  # the operator view must never stop the run
                self.errors += 1
                self.last_error = str(e)[:160]

    def _heartbeat(self) -> None:
        while not self._stop.wait(HEARTBEAT_S):
            if self.latest:
                self._ship([{**r, "twin.heartbeat": True} for r in self.latest])

    def close(self) -> None:
        self._stop.set()
