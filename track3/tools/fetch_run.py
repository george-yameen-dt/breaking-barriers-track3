"""Pulls one run's audit records (DynamoDB) and its declaration (S3) into evidence/, read-only.

    python3 tools/fetch_run.py <run_id> [<run_id> ...]

Then `python3 tools/rebuild.py` scores it from the files alone.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGION, TABLE, BUCKET = "us-east-2", "bb26-team04-track3-records", "bb26-p-team04-data-279760855394-use2"


def records_from_items(items: list) -> list:
    """The run's records in order; the counter row (seq 0000) has none."""
    rows = [json.loads(i["record"]["S"]) for i in items if "record" in i]
    return sorted(rows, key=lambda r: r["seq"])


def write_records(root: Path, run_id: str, records: list) -> Path:
    path = root / "evidence" / "runs" / run_id / "records.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    return path


def fetch(run_id: str) -> None:
    import boto3
    ddb, s3 = boto3.client("dynamodb", region_name=REGION), boto3.client("s3", region_name=REGION)
    items, kwargs = [], {"TableName": TABLE, "KeyConditionExpression": "run_id = :r", "ConsistentRead": True,
                         "ExpressionAttributeValues": {":r": {"S": run_id}}}
    while True:
        page = ddb.query(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    recs = records_from_items(items)
    declared = ROOT / "evidence" / "declared" / f"{run_id}.json"
    declared.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(BUCKET, f"evidence/declared/{run_id}.json", str(declared))
    print(f"{run_id}: {len(recs)} records -> {write_records(ROOT, run_id, recs).relative_to(ROOT)}; declaration -> {declared.relative_to(ROOT)}")


if __name__ == "__main__":
    for rid in sys.argv[1:] or sys.exit(__doc__):
        fetch(rid)
