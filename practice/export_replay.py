"""Exports one real recorded run from yqs73977 for the shareable demo pages (replay + Cortex view).

Picks the newest problem the ops agent rolled back, and writes demo-pages/data.js with:
the step frames around it (thinned to 2 per second), the story timeline, Claude's answer,
app-experience numbers, and the findings registry across every problem the agent handled.
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path(__file__).parent / "demo-pages" / "data.js"
# dtctl uses its current context (dtctl config use-context <name>); set DTCTL_CONTEXT to pin another one.
DTCTL = ["dtctl", *(["--context", os.environ["DTCTL_CONTEXT"]] if os.environ.get("DTCTL_CONTEXT") else [])]


def dql(query):
    out = subprocess.run([*DTCTL, "query", query, "-o", "json", "--agent", "--max-field-chars", "0"],
                         capture_output=True, text=True, timeout=120)
    reply = json.loads(out.stdout)
    if not reply.get("ok"):
        raise RuntimeError(out.stdout[:400])
    body = reply["result"]
    if body.get("kind") == "result-file":
        return [json.loads(line) for line in open(body["path"])]
    return body.get("records") or []


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")[:26] + "+00:00" if "." in s else s.replace("Z", "+00:00"))


def iso(d):
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def main():
    runs = dql('fetch logs, from: now()-6h | filter service.name == "cell-ops-agent" and (isNull(agent.state) or in(agent.state, {"acted", "advised"})) '
               '| sort timestamp desc | fields timestamp, content, agent.problem, agent.root_cause, agent.evidence, '
               'agent.action, agent.acted, agent.model, agent.input_tokens, agent.output_tokens, agent.cost_usd, agent.duration_ms')
    acted = [r for r in runs if str(r.get("agent.acted")).lower() == "true"]
    run = acted[0]
    pid = run["agent.problem"]
    problem = dql(f'fetch dt.davis.problems, from: now()-6h | filter display_id == "{pid}" '
                  '| fields display_id, event.start, event.end, event.status')[0]
    p_start = ts(problem["event.start"])
    fixed_at = ts(run["timestamp"])
    last_v1 = dql(f'fetch logs, from: "{iso(p_start - timedelta(minutes=10))}", to: "{iso(p_start)}" '
                  '| filter service.name == "mobile-env" and mobile.kind == "step" and mobile.policy == "v1" '
                  '| summarize t = max(timestamp)')[0]["t"]
    v2_on = ts(dql(f'fetch logs, from: "{last_v1}", to: "{iso(p_start + timedelta(minutes=2))}" '
                   '| filter service.name == "mobile-env" and mobile.kind == "step" and mobile.policy == "v2" '
                   '| summarize t = min(timestamp)')[0]["t"])
    first_drop = ts(dql(f'fetch logs, from: "{iso(v2_on)}", to: "{iso(fixed_at)}" '
                        '| filter service.name == "mobile-env" and mobile.kind == "safety" | summarize t = min(timestamp)')[0]["t"])

    start, end = v2_on - timedelta(seconds=25), fixed_at + timedelta(seconds=40)
    steps = dql(f'fetch logs, from: "{iso(start)}", to: "{iso(end)}" '
                '| filter service.name == "mobile-env" and mobile.kind == "step" | sort timestamp asc '
                '| fields timestamp, mobile.policy, mobile.step, mobile.phones_active, mobile.phones_served, '
                'mobile.phones_dropped, mobile.ues, mobile.towers')
    frames, last_t = [], None
    towers = json.loads(steps[0]["mobile.towers"])
    for s in steps:
        t = ts(s["timestamp"])
        if last_t and (t - last_t).total_seconds() < 0.5:
            continue
        last_t = t
        frames.append({
            "t": round((t - start).total_seconds(), 2),
            "m": s["mobile.policy"],
            "d": int(s["mobile.phones_dropped"]),
            "s": int(s["mobile.phones_served"]),
            "a": int(s["mobile.phones_active"]),
            "p": [[u["id"], u["x"], u["y"], {"served": 0, "dropped": 1}.get(u["state"], 2), u["links"], u["rate"]]
                  for u in json.loads(s["mobile.ues"])],
        })

    exp = dql('fetch logs, from: now()-6h | filter service.name == "mobile-env" and mobile.kind == "step" '
              '| parse mobile.ues, "JSON_ARRAY:ues" | expand ues | fieldsAdd rate = toDouble(ues[rate]) '
              '| summarize call = 100.0 * countIf(rate >= 0.1) / count(), reels = 100.0 * countIf(rate >= 3) / count(), '
              'by: {policy = mobile.policy}')

    # Findings registry: every handled problem, with the towers its cut-off phones sat next to.
    findings = []
    for r in runs:
        p = dql(f'fetch dt.davis.problems, from: now()-6h | filter display_id == "{r["agent.problem"]}" '
                '| fields event.start, event.end')
        if not p:
            continue
        a, b = ts(p[0]["event.start"]), ts(r["timestamp"])
        # The moment the problem started: one snapshot (the first safety records), so each cut-off phone counts once.
        first = dql(f'fetch logs, from: "{iso(a - timedelta(seconds=5))}", to: "{iso(a + timedelta(seconds=60))}" '
                    '| filter service.name == "mobile-env" and mobile.kind == "safety" | summarize t = min(timestamp)')
        towers_hit = []
        if first and first[0].get("t"):
            t0 = ts(first[0]["t"])
            towers_hit = dql(f'fetch logs, from: "{iso(t0)}", to: "{iso(t0 + timedelta(milliseconds=500))}" '
                             '| filter service.name == "mobile-env" and mobile.kind == "safety" and isNotNull(cell.nearest_tower) '
                             '| summarize phones = countDistinct(cell.phone.id), by: {tower = cell.nearest_tower} | sort tower asc')
        findings.append({
            "problem": r["agent.problem"], "at": r["timestamp"], "summary": r["content"],
            "root": r.get("agent.root_cause", ""), "action": r.get("agent.action", ""),
            "acted": str(r.get("agent.acted")).lower() == "true",
            "towers": {x["tower"]: int(x["phones"]) for x in towers_hit},
            "tokens": int(r.get("agent.input_tokens") or 0) + int(r.get("agent.output_tokens") or 0),
            "cost": float(r.get("agent.cost_usd") or 0), "secs": round(int(r.get("agent.duration_ms") or 0) / 1000, 1),
        })

    data = {
        "recordedAt": run["timestamp"], "tenant": "yqs73977",
        "problem": pid, "problemUrl": f"https://yqs73977.sprint.apps.dynatracelabs.com/ui/apps/dynatrace.davis.problems/problem/{pid}",
        "marks": {
            "v2On": round((v2_on - start).total_seconds(), 1),
            "firstDrop": round((first_drop - start).total_seconds(), 1),
            "problem": round((p_start - start).total_seconds(), 1),
            "fixed": round((fixed_at - start).total_seconds(), 1),
            "end": round((end - start).total_seconds(), 1),
        },
        "advice": {
            "summary": run["content"], "root": run.get("agent.root_cause", ""),
            "evidence": json.loads(run.get("agent.evidence") or "[]"), "model": run.get("agent.model", ""),
            "tokens": int(run.get("agent.input_tokens") or 0) + int(run.get("agent.output_tokens") or 0),
            "cost": float(run.get("agent.cost_usd") or 0), "secs": round(int(run.get("agent.duration_ms") or 0) / 1000, 1),
        },
        "apps": {r["policy"]: {"call": round(r["call"]), "reels": round(r["reels"])} for r in exp},
        "towers": [[t["name"], t["x"], t["y"], t["range"]] for t in towers],
        "map": [200, 300],
        "frames": frames,
        "findings": findings,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("window.DEMO = " + json.dumps(data, separators=(",", ":")) + ";\n")
    print(f"{pid}: {len(frames)} frames over {data['marks']['end']} s · v2 on {data['marks']['v2On']} s · "
          f"problem {data['marks']['problem']} s · fixed {data['marks']['fixed']} s · {len(findings)} findings · "
          f"{OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
