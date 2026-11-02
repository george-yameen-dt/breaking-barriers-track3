"""Read-only Dynatrace access for the agents through dtctl, plus the Dynatrace for AI skills.

Every query is parsed here first (command, time window, data object, any write), so local Cedar decides on facts the
model did not write (policies/swarm.cedar: dql-read-shape). dtctl's readonly safety level and the token's read-only
scopes are the next fences. Skills come from the Dynatrace for AI plugin (a packaged copy in skills/ on AgentCore).
"""

import json
import os
import re
import subprocess
from pathlib import Path

from contracts import SKILLS, sha256_of

CONTEXT = os.environ.get("DTCTL_CONTEXT", "astroshop")
DEFAULT_WINDOW_MIN = 120
SKILL_DIRS = [Path(__file__).parent / "skills",
              *sorted((Path.home() / ".claude/plugins/cache/dynatrace-for-ai/dynatrace").glob("*/skills"), reverse=True)]
LOG_KINDS = ("cell", "phone", "tower", "safety", "step")
_UNITS = {"s": 1 / 60, "m": 1, "h": 60, "d": 1440}
_WRITES = re.compile(r"\b(delete|insert|update|upsert|ingest|drop|create|alter|grant)\b", re.I)


def parse_dql(dql: str) -> dict:
    """Facts about a query for Cedar: its command, how far back it reads, what it reads, and whether it writes."""
    text = (dql or "").strip()
    first = re.match(r"(\w+)\s*([\w.]*)", text)
    verb = first.group(1) if first else ""
    data_object = first.group(2) if first and verb.lower() == "fetch" else ("metrics" if verb.lower() == "timeseries" else "")
    window = DEFAULT_WINDOW_MIN
    starts = re.findall(r"\bfrom\s*:", text)
    simple = re.fullmatch(r".*?\bfrom\s*:\s*now\(\)\s*-\s*(\d+)\s*([smhd])\s*(?:[,|].*)?", text, re.S)
    if "timeframe" in text or "[" in text or len(starts) > 1 or (starts and not simple):
        window = 99999  # a timeframe, a subquery, several or unusual start times: treat it as unbounded
    elif simple:
        window = int(round(int(simple.group(1)) * _UNITS[simple.group(2)]))
    return {"dql_verb": verb, "window_minutes": window, "data_object": data_object, "has_write": bool(_WRITES.search(text))}


def query(dql: str, traceparent: str = "", max_rows: int = 40) -> dict:
    """One DQL query through dtctl. --agent fixes the JSON shape (dtctl prints another shape outside AI tools)."""
    env = {**os.environ, **({"TRACEPARENT": traceparent} if traceparent else {})}
    try:
        out = subprocess.run(["dtctl", "--context", CONTEXT, "query", dql, "-o", "json", "--agent"],
                             capture_output=True, text=True, timeout=60, env=env)
        reply = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return {"dql": dql, "error": f"dtctl failed: {str(e)[:200]}"}
    if not reply.get("ok"):
        return {"dql": dql, "error": f"dtctl: {str(reply.get('error') or out.stderr or out.stdout)[:300]}"}
    body = reply.get("result") or {}
    rows = [json.loads(line) for line in open(body["path"])] if body.get("kind") == "result-file" else body.get("records") or []
    return {"dql": dql, "row_count": len(rows), "rows": rows[:max_rows], "result_sha256": sha256_of(rows)}


def _window(minutes, default: int) -> int:
    try:
        return max(1, min(int(minutes), DEFAULT_WINDOW_MIN))
    except (TypeError, ValueError):
        return default


def problems(window_minutes=120) -> dict:
    # Filter on ACTIVE: closed problems get late updates and would otherwise hide a newer open one.
    return query(f"fetch dt.davis.problems, from: now()-{_window(window_minutes, 120)}m | filter event.status == \"ACTIVE\" "
                 "| sort event.start desc | limit 20 | fields display_id, event.name, event.start, event.description")


def log_patterns(kind: str, window_minutes=30) -> dict:
    if kind not in LOG_KINDS:
        return {"error": f"kind must be one of {', '.join(LOG_KINDS)}"}
    return query(f"fetch logs, from: now()-{_window(window_minutes, 30)}m | filter service.name == \"mobile-env\" "
                 f"and mobile.kind == \"{kind}\" | summarize count = count(), by: {{content}} | sort count desc | limit 15")


def load_skill(name: str, max_chars: int = 4000) -> dict:
    if name not in SKILLS:
        return {"error": f"skill must be one of {', '.join(SKILLS)}"}
    for d in SKILL_DIRS:
        f = d / name / "SKILL.md"
        if f.exists():
            return {"skill": name, "text": f.read_text()[:max_chars]}
    return {"error": f"skill {name} is not installed (Dynatrace for AI plugin)"}
