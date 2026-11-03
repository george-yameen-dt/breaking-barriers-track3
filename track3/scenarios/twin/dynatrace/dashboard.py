# "Viavi twin (team04)" dashboard on yqs73977 (dtctl context astroshop): the tower metrics the twin pipeline extracts
# (pipeline.json, metricExtraction), the twin's problem, the agents' changes and the tower logs, for the end-to-end demo.
#   python3 dashboard.py [--validate]  -> dashboard.json ; deploy: dtctl --context astroshop apply -f dashboard.json
import json
import subprocess
import sys

CTX = "astroshop"
QS = {"defaultSamplingRatio": 1, "defaultScanLimitGbytes": -1, "enableSampling": False,
      "maxResultMegaBytes": 20, "maxResultRecords": 1000}
COLORS = {"ok": "var(--dt-colors-charts-status-ideal-default, #2f6862)",
          "warn": "var(--dt-colors-charts-status-warning-default, #eea53c)",
          "crit": "var(--dt-colors-charts-status-critical-default, #c62239)"}


def md(text): return {"type": "markdown", "content": text}


def tile(title, query, vis, vs=None):
    return {"title": title, "type": "data", "query": query, "visualization": vis,
            "visualizationSettings": {"autoSelectVisualization": False, **(vs or {})}, "querySettings": QS}


def thresholds(field, rules):  # rules: (comparator, value, ok|warn|crit)
    return [{"id": 1, "field": field, "title": "", "isEnabled": True,
             "rules": [{"id": i, "color": {"Default": COLORS[c]}, "comparator": cmp, "label": "", "value": v}
                       for i, (cmp, v, c) in enumerate(rules)]}]


def kpi(title, query, label, thr):
    sv = {"label": label, "recordField": "now", "colorThresholdTarget": "background",
          "sparklineSettings": {"isVisible": True, "record": "series"}, "trend": {"isVisible": False}}
    return tile(title, query, "singleValue", {"singleValue": sv, "thresholds": thr})


def latest(metric, filt=""):  # the newest minute plus the series behind it, for a single value with a sparkline
    return (f"timeseries series = avg({metric}){filt}, interval:1m, from:now()-30m\n"
            "| fieldsAdd now = arrayLast(arrayRemoveNulls(series))")


def towers(metric):
    return f"timeseries value = avg({metric}), by:{{twin.cell_name}}, interval:1m"


QOS = thresholds("now", [("≥", 0, "crit"), ("≥", 60, "warn"), ("≥", 90, "ok")])
T, L = {}, {}
def put(k, t, x, y, w, h): T[k] = t; L[k] = {"x": x, "y": y, "w": w, "h": h}

put("hdr", md("## Viavi twin (team04) · live network\n"
              "Healthy = **QoS 100** on every tower (every phone gets the speed it asked for). "
              "A tower goes down → Dynatrace opens a problem on it → the agents propose a fix → a person approves → "
              "the twin measures the result."), 0, 0, 24, 2)
for i, c in enumerate(("S1", "S2", "S3", "S4")):
    put(f"q{c}", kpi(f"Tower {c}", latest("log.viavi_twin.qos_score", f', filter: twin.cell_name == "Tower {c}"'),
                     "QoS now", QOS), i * 4, 2, 4, 3)
put("net", kpi("Whole network", latest("log.viavi_twin.network.qos_score"), "QoS now", QOS), 16, 2, 4, 3)
put("drop", kpi("Phones without service", latest("log.viavi_twin.network.phones_without_service"), "now",
                thresholds("now", [("≥", 0, "ok"), ("≥", 10, "warn"), ("≥", 13, "crit")])), 20, 2, 4, 3)
put("qos", tile("QoS Score by tower", towers("log.viavi_twin.qos_score"), "lineChart"), 0, 5, 12, 6)
put("load", tile("Tower load %", towers("log.viavi_twin.load_pct"), "lineChart"), 12, 5, 6, 6)
put("pwr", tile("Transmit power (dBm): the agents' lever", towers("log.viavi_twin.tx_power_dbm"), "lineChart"), 18, 5, 6, 6)
put("prob", tile("Problems",
                 'fetch dt.davis.problems, from:now()-2h | filter contains(event.name, "Viavi twin")\n'
                 '| dedup display_id, sort:{timestamp desc}\n'
                 '| fields started = event.start, problem = display_id, status = event.status, what = event.name\n'
                 '| sort started desc | limit 6', "table"), 0, 11, 9, 5)
put("chg", tile("Agents' changes (approved by a person, checked by Cedar)",
                'fetch logs, from:now()-2h | filter service.name == "viavi-twin" and twin.kind == "change"\n'
                '| fields time = timestamp, tower = twin.cell, setting = twin.parameter, from = twin.old_value, '
                'to = twin.new_value, accepted = twin.accepted\n| sort time desc | limit 6', "table"), 9, 11, 15, 5)
put("logs", tile("Tower logs (tagged with the tower in Smartscape)",
                 'fetch logs, from:now()-30m | filter service.name == "viavi-twin" and isNotNull(dt.smartscape_source.id)\n'
                 '| filter isNull(twin.heartbeat)\n'
                 '| fields timestamp, loglevel, tower = twin.cell_name, content\n| sort timestamp desc | limit 30',
                 "table"), 0, 16, 24, 6)

dash = {"id": "c36c542f-96f1-4115-b6d4-d525f27d975c", "name": "Viavi twin (team04): towers, problems, agent fixes",
        "type": "dashboard",
        "content": {"version": 21, "variables": [], "tiles": T, "layouts": L,
                    "settings": {"defaultTimeframe": {"enabled": True, "value": {"from": "now()-30m", "to": "now()"}},
                                 "gridColumnsCount": 24, "syncTimeframeAcrossTiles": True}}}
json.dump(dash, open("dashboard.json", "w"), indent=1, ensure_ascii=False)
if "--validate" in sys.argv:
    for k, t in T.items():
        if t["type"] == "data":
            r = subprocess.run(["dtctl", "--context", CTX, "query", t["query"], "-o", "json", "--plain"],
                               capture_output=True, text=True)
            print(k, "OK" if r.returncode == 0 else "FAIL " + (r.stderr or r.stdout)[:300], "|", t["title"])
