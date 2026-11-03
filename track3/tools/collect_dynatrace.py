"""What Dynatrace did about a fault, copied into the run's audit records.

The judges' live test can read S3, CloudWatch Logs Insights and DynamoDB only, so the facts the Dynatrace-side units
(detect, one_problem) need are read here, with dtctl and read-only DQL, and written to the run as one
`dynatrace_evidence` record under the run's own run id and trace id.

    python3 tools/collect_dynatrace.py --run-id <the swarm run> --twin-run-id <the poller's run id> [--window 10]
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dt_tools  # noqa: E402
from contracts import sha256_of  # noqa: E402


def fault_dql(twin_run_id: str) -> str:
    return (f'fetch logs, from: now()-12h | filter service.name == "viavi-twin" and twin.run_id == "{twin_run_id}" '
            'and twin.phase == "fault" | sort timestamp asc | limit 1 | fields timestamp')


def problems_dql(fault_start: str, window_minutes: int) -> str:
    start = datetime.fromisoformat(fault_start.replace("Z", "+00:00")) - timedelta(minutes=1)
    end = start + timedelta(minutes=window_minutes + 1)
    return (f'fetch dt.davis.problems, from: "{start:%Y-%m-%dT%H:%M:%SZ}", to: "{end:%Y-%m-%dT%H:%M:%SZ}" '
            '| filter matchesPhrase(event.name, "Viavi twin") | summarize event.start = min(event.start), '
            "event.name = takeFirst(event.name), event.status = takeLast(event.status), by: {display_id} | sort event.start asc")


def fault_start(rows: list) -> str | None:
    stamps = [r["timestamp"] for r in rows if r.get("timestamp")]
    return min(stamps) if stamps else None


def evidence(fault_start_: str, problems: list, window_minutes: int) -> dict:
    return {"fault_start": fault_start_, "window_minutes": window_minutes, "problem_count": len(problems),
            "problems": [{k: p.get(k) for k in ("display_id", "event.name", "event.start", "event.status")} for p in problems],
            "query_sha256": sha256_of(problems)[-64:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--twin-run-id", required=True)
    ap.add_argument("--window", type=int, default=10, help="minutes after the fault in which problems count")
    ap.add_argument("--dry-run", action="store_true", help="print the record, write nothing")
    a = ap.parse_args()
    first = dt_tools.query(fault_dql(a.twin_run_id))
    start = fault_start(first.get("rows", []))
    if not start:
        print(f"no fault-phase record for twin run {a.twin_run_id}: {first.get('error', '')}")
        return 1
    found = dt_tools.query(problems_dql(start, a.window), max_rows=50)
    rec = evidence(start, found.get("rows", []), a.window)
    rec["twin_run_id"] = a.twin_run_id
    print(json.dumps(rec, indent=1))
    if a.dry_run:
        return 0
    from agents.governance import SwarmCheckpoint
    import boto3
    from checkpoint import RECORDS_TABLE, REGION
    ddb = boto3.client("dynamodb", region_name=REGION)
    first_item = ddb.query(TableName=RECORDS_TABLE, KeyConditionExpression="run_id = :r AND seq = :s",
                           ExpressionAttributeValues={":r": {"S": a.run_id}, ":s": {"S": "0001"}})["Items"]
    if not first_item:
        print(f"no records for run {a.run_id}")
        return 1
    cp = SwarmCheckpoint(a.run_id, first_item[0]["trace_id"]["S"], Path(__file__).resolve().parent.parent / "dev-runs" / a.run_id)
    cp.record("dynatrace_evidence", **rec)
    cp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
