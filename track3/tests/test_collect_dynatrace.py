"""tools/collect_dynatrace.py: what Dynatrace did about a fault, copied into the audit records so a judge can read it in AWS."""

from tools import collect_dynatrace as cd


def test_fault_start_is_the_first_fault_record():
    rows = [{"timestamp": "2026-11-03T10:03:00Z"}, {"timestamp": "2026-11-03T10:00:30Z"}]
    assert cd.fault_start(rows) == "2026-11-03T10:00:30Z"
    assert cd.fault_start([]) is None


def test_the_evidence_record_is_built_from_the_query_rows():
    problems = [{"display_id": "P-1", "event.name": "Cell dark", "event.start": "2026-11-03T10:01:00Z", "event.status": "CLOSED"},
                {"display_id": "P-2", "event.name": "Cell QoS dropped", "event.start": "2026-11-03T10:01:20Z", "event.status": "ACTIVE"}]
    ev = cd.evidence("2026-11-03T10:00:30Z", problems, window_minutes=10)
    assert ev["problem_count"] == 2 and ev["fault_start"] == "2026-11-03T10:00:30Z" and ev["window_minutes"] == 10
    assert [p["display_id"] for p in ev["problems"]] == ["P-1", "P-2"] and len(ev["query_sha256"]) == 64


def test_the_queries_are_read_only_and_bounded():
    assert "fetch" in cd.problems_dql("2026-11-03T10:00:30Z", 10) and "delete" not in cd.problems_dql("2026-11-03T10:00:30Z", 10).lower()
    assert 'twin.run_id == "run-1"' in cd.fault_dql("run-1")
