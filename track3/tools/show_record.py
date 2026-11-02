"""Read-only record viewer for the judges' live test: Amazon S3, CloudWatch Logs Insights and DynamoDB only.

  python3 tools/show_record.py --run-id <run_id>              every record of the run, from DynamoDB
  python3 tools/show_record.py --run-id <run_id> --item 7     one record
  python3 tools/show_record.py --run-id <run_id> --trace      the run's trace, rebuilt from CloudWatch Logs Insights
"""

import argparse
import json
import time

import boto3

REGION = "us-east-2"
BUCKET = "bb26-p-team04-data-279760855394-use2"
LOG_GROUP = "/bb26/team04/track3"
TABLE = "bb26-team04-track3-records"


def line(r: dict) -> str:
    keys = ("action", "decision", "policies", "tool", "who", "model_id", "approver", "approved", "status", "score", "reason", "result")
    extra = " ".join(f"{k}={json.dumps(r[k]) if not isinstance(r[k], str) else r[k]}" for k in keys if k in r and r[k] not in ("", [], None))
    return f"{r['seq']:>3}  {r['time']}  {r['kind']:<18} {extra}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--item", type=int)
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()

    s3 = boto3.client("s3", region_name=REGION)
    decl = json.loads(s3.get_object(Bucket=BUCKET, Key=f"evidence/declared/{a.run_id}.json")["Body"].read())
    head = s3.head_object(Bucket=BUCKET, Key=f"evidence/declared/{a.run_id}.json")
    print(f"declared {decl['declared_at']} (S3 LastModified {head['LastModified'].isoformat()}) · units {decl['units']}")
    print(f"targets: {json.dumps(decl['target'])}\n")

    if a.trace:
        logs = boto3.client("logs", region_name=REGION)
        q = logs.start_query(logGroupName=LOG_GROUP, startTime=int(time.time()) - 3 * 86400, endTime=int(time.time()) + 60,
                             queryString=f'fields @timestamp, @message | filter @logStream = "{a.run_id}" | sort @timestamp asc | limit 500')
        while (res := logs.get_query_results(queryId=q["queryId"]))["status"] in ("Running", "Scheduled"):
            time.sleep(1)
        recs = [json.loads(next(f["value"] for f in row if f["field"] == "@message")) for row in res["results"]]
        print(f"CloudWatch Logs Insights · {len(recs)} records · trace ids {sorted({r['trace_id'] for r in recs})}")
    else:
        ddb = boto3.client("dynamodb", region_name=REGION)
        kw = {"TableName": TABLE, "KeyConditionExpression": "run_id = :r", "ExpressionAttributeValues": {":r": {"S": a.run_id}}}
        if a.item:
            kw["KeyConditionExpression"] += " AND seq = :s"
            kw["ExpressionAttributeValues"][":s"] = {"S": f"{a.item:04d}"}
        recs = [json.loads(i["record"]["S"]) for i in ddb.query(**kw)["Items"]]
        print(f"DynamoDB {TABLE} · {len(recs)} records")
    for r in recs:
        print(line(r) if not a.item else json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
