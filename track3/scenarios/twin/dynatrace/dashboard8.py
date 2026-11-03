# "Viavi twin: 8-minute test" dashboard on yqs73977 (dtctl context astroshop), for scenarios/twin/run8.py:
# 3 healthy minutes, then S3 loses power at 180 s. One point per minute per tower, the problem, the twin's events.
#   python3 dashboard8.py [--validate]  -> dashboard8.json ; first deploy: dtctl --context astroshop apply -f dashboard8.json
import json
import subprocess
import sys

from dashboard import CTX, QOS, kpi, latest, md, thresholds, tile, towers

COV = thresholds("now", [("≥", 0, "crit"), ("≥", 70, "warn"), ("≥", 75, "ok")])
RUN = 'filter service.name == "viavi-twin" and isNotNull(twin.phase)'
T, L = {}, {}
def put(k, t, x, y, w, h): T[k] = t; L[k] = {"x": x, "y": y, "w": w, "h": h}

put("hdr", md("## Viavi twin · 8-minute test\n"
              "Minutes 1–3 **healthy**, minutes 4–8 **S3 has lost power** (it can't be switched back on remotely). "
              "One point per minute. Run it with `scenarios/twin/run8.py`."), 0, 0, 24, 2)
put("cov", kpi("Coverage now", latest("log.viavi_twin.network.coverage_pct"), "% of phones with signal", COV), 0, 2, 6, 3)
put("qos", kpi("Network QoS now", latest("log.viavi_twin.network.qos_score"), "QoS Score", QOS), 6, 2, 6, 3)
put("drop", kpi("Phones without service", latest("log.viavi_twin.network.phones_without_service"), "now",
                thresholds("now", [("≥", 0, "ok"), ("≥", 10, "warn"), ("≥", 13, "crit")])), 12, 2, 6, 3)
put("phase", tile("Phase now", f'fetch logs, from:now()-15m | {RUN} and twin.kind == "network"\n'
                  '| sort timestamp desc | limit 1 | fields phase = twin.phase, minute = twin.minute, run = twin.run_id',
                  "table"), 18, 2, 6, 3)
put("covl", tile("Coverage % (healthy, then the power cut)",
                 "timeseries coverage = avg(log.viavi_twin.network.coverage_pct), interval:1m", "lineChart"), 0, 5, 12, 6)
put("qosl", tile("QoS Score by tower", towers("log.viavi_twin.qos_score"), "lineChart"), 12, 5, 12, 6)
put("load", tile("Tower load %", towers("log.viavi_twin.load_pct"), "lineChart"), 0, 11, 12, 5)
put("ph", tile("Phones per tower (S3's phones move to its neighbours)", towers("log.viavi_twin.phones"), "lineChart"),
    12, 11, 12, 5)
put("min", tile("Minute by minute",
                f'fetch logs, from:now()-30m | {RUN} and twin.kind == "network" and isNull(twin.heartbeat)\n'
                '| fields minute = twin.minute, phase = twin.phase, `coverage %` = twin.coverage_pct, '
                '`without service` = twin.phones_without_service, qos = twin.qos_score, dark = twin.dark_cells\n'
                '| sort minute asc | limit 20', "table"), 0, 16, 12, 6)
put("ev", tile("Twin events (fault raised, tower off, lost links)",
               f'fetch logs, from:now()-30m | {RUN} and twin.kind == "event"\n'
               '| fields minute = twin.minute, tower = twin.cell_name, event = twin.event_type, content\n'
               '| sort minute asc | limit 30', "table"), 12, 16, 12, 6)
put("prob", tile("Problems",
                 'fetch dt.davis.problems, from:now()-2h | filter contains(event.name, "Viavi twin")\n'
                 '| dedup display_id, sort:{timestamp desc}\n'
                 '| fields started = event.start, problem = display_id, status = event.status, what = event.name\n'
                 '| sort started desc | limit 5', "table"), 0, 22, 24, 4)

dash = {"id": "31c9ec07-ddc0-416c-ab6f-59376a129ff9", "name": "Viavi twin (team04): 8-minute test", "type": "dashboard",
        "content": {"version": 21, "variables": [], "tiles": T, "layouts": L,
                    "settings": {"defaultTimeframe": {"enabled": True, "value": {"from": "now()-15m", "to": "now()"}},
                                 "gridColumnsCount": 24, "syncTimeframeAcrossTiles": True}}}
json.dump(dash, open("dashboard8.json", "w"), indent=1, ensure_ascii=False)
if "--validate" in sys.argv:
    for k, t in T.items():
        if t["type"] == "data":
            r = subprocess.run(["dtctl", "--context", CTX, "query", t["query"], "-o", "json", "--plain"],
                               capture_output=True, text=True)
            print(k, "OK" if r.returncode == 0 else "FAIL " + (r.stderr or r.stdout)[:300], "|", t["title"])
