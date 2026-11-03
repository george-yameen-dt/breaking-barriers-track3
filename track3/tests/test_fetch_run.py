"""tools/fetch_run.py: a run's audit records and declaration, pulled from AWS into evidence/."""

import json

from tools import fetch_run


def test_records_come_back_in_order_without_the_counter_row():
    items = [{"seq": {"S": "0002"}, "record": {"S": json.dumps({"seq": 2, "kind": "b"})}},
             {"seq": {"S": "0000"}},  # the run's atomic counter row has no record
             {"seq": {"S": "0001"}, "record": {"S": json.dumps({"seq": 1, "kind": "a"})}}]
    assert [r["kind"] for r in fetch_run.records_from_items(items)] == ["a", "b"]


def test_records_are_written_one_json_object_per_line(tmp_path):
    path = fetch_run.write_records(tmp_path, "run-1", [{"seq": 1, "kind": "a"}, {"seq": 2, "kind": "b"}])
    assert path == tmp_path / "evidence" / "runs" / "run-1" / "records.jsonl"
    assert [json.loads(line)["kind"] for line in path.read_text().splitlines()] == ["a", "b"]
