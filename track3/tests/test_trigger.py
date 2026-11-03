"""The Dynatrace workflow's Lambda: one run per problem, a stable trace id, and an alert text that names the cells."""

import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

TRACK3 = Path(__file__).resolve().parent.parent


@pytest.fixture
def lam(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_ARN", "arn:aws:bedrock-agentcore:us-east-2:1:runtime/x")
    monkeypatch.setenv("TRIGGER_TOKEN", "secret")
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: MagicMock())
    spec = importlib.util.spec_from_file_location("trigger_lambda_under_test", TRACK3 / "aws" / "trigger_lambda.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    store = {}

    def put_item(TableName, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        key = (Item["run_id"]["S"], Item["seq"]["S"])
        old = store.get(key)
        if old is not None:
            expired = ExpressionAttributeValues and int(old["claimed_epoch"]["N"]) < int(ExpressionAttributeValues[":cutoff"]["N"])
            if not expired:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")
        store[key] = Item

    def get_item(TableName, Key, ConsistentRead=True):
        return {"Item": store[(Key["run_id"]["S"], Key["seq"]["S"])]}

    mod.ddb = SimpleNamespace(put_item=put_item, get_item=get_item)
    mod.lam = MagicMock()
    mod.s3 = SimpleNamespace(put_object=MagicMock(), head_object=lambda **k: {"LastModified": datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)})
    return mod


def event(problem_id="P-2611-1", **extra):
    body = {"problem_id": problem_id, "title": "Cell QoS dropped", "status": "ACTIVE", **extra}
    return {"headers": {"X-Track3-Token": "secret"}, "body": json.dumps(body)}


CTX = SimpleNamespace(function_name="track3-trigger")


def test_trace_id_is_stable_for_a_problem(lam):
    a, b, c = lam.start_run({"problem_id": "P-1"}), lam.start_run({"problem_id": "P-1"}), lam.start_run({"problem_id": "P-2"})
    assert a["trace_id"] == b["trace_id"] != c["trace_id"]
    assert len(a["trace_id"]) == 32 and int(a["trace_id"], 16) >= 0


def test_duplicate_problem_starts_one_run(lam):
    first = lam.handler(event(), CTX)
    second = lam.handler(event(), CTX)
    assert first["statusCode"] == 202 and second["statusCode"] == 200
    one, two = json.loads(first["body"]), json.loads(second["body"])
    assert one["run_id"] == two["run_id"] and one["trace_id"] == two["trace_id"] and two["duplicate"] is True
    assert lam.lam.invoke.call_count == 1


def test_wrong_token_is_refused(lam):
    e = event()
    e["headers"] = {"x-track3-token": "nope"}
    assert lam.handler(e, CTX)["statusCode"] == 401 and lam.lam.invoke.call_count == 0


def test_affected_cells_come_from_the_problem_text(lam):
    p = {"problem_id": "P-1", "description": "S4/B3/C1 (criticality emergency) has a QoS score below 90; S3/B3/C1 stopped transmitting"}
    assert lam.affected_cells(p) == ["S4/B3/C1", "S3/B3/C1"]
    assert lam.affected_cells({"problem_id": "P-1", "affected_cells": ["S2/B3/C1"]}) == ["S2/B3/C1"]
    assert lam.affected_cells({"problem_id": "P-1"}) == []


def test_alert_text_names_cells_and_criticality(lam):
    text = lam.alert_text({"problem_id": "P-1", "title": "Cell dark", "affected_cells": ["S3/B3/C1", "S4/B3/C1", "S2/B3/C1"]})
    assert "S4/B3/C1 (emergency)" in text and "S2/B3/C1 (hospital)" in text and "S3/B3/C1 (none)" in text
    assert "Cell dark" in text and "P-1" in text


def test_the_payload_carries_the_cells(lam):
    payload = lam.start_run({"problem_id": "P-1", "description": "S4/B3/C1 is below 90"})
    assert payload["affected_cells"] == ["S4/B3/C1"] and "S4/B3/C1 (emergency)" in payload["alert_text"]


# ---- the declared targets are written to S3 before the run starts ----------------------------------
def test_the_targets_are_declared_in_s3_before_the_orchestrator_starts(lam):
    order = []
    lam.s3 = SimpleNamespace(put_object=lambda **k: order.append(("s3", k)),
                             head_object=lambda **k: {"LastModified": __import__("datetime").datetime(2026, 11, 3, 10, 0, tzinfo=__import__("datetime").timezone.utc)})
    lam.lam.invoke.side_effect = lambda **k: order.append(("invoke", k))
    out = lam.handler(event(), CTX)
    assert out["statusCode"] == 202 and [k for k, _ in order] == ["s3", "invoke"], [k for k, _ in order]
    run_id = json.loads(out["body"])["run_id"]
    put = order[0][1]
    assert put["Key"] == f"evidence/declared/{run_id}.json"
    decl = json.loads(put["Body"])
    assert decl["run_id"] == run_id
    assert {"critical_safe", "problem_to_command", "swarm_autonomous_apply", "swarm_verify"} <= set(decl["units"])
    assert not {"detect", "one_problem"} & set(decl["units"]), "operator-attested checks are not declared units"
    assert decl["test_set"] == "scenarios/twin/healthy-power-cut.json" and len(decl["test_set_sha256"]) == 64
    sent = json.loads(order[1][1]["Payload"])["_run"]
    assert sent["declared"] is True and sent["declared_s3"].startswith("2026-11-03T10:00")


def test_a_duplicate_problem_declares_nothing(lam):
    lam.handler(event(), CTX)
    lam.s3.put_object.reset_mock()
    lam.handler(event(), CTX)
    lam.s3.put_object.assert_not_called()


def test_the_declared_units_exist_and_the_inputs_hash(lam):
    from tools import check
    assert set(lam.UNITS) <= set(check.UNITS)
    doc = lam.declared_doc({"run_id": "r", "problem_id": "P-1", "trace_id": "a" * 32})
    assert doc["policies_sha256"] and set(doc["target"]) >= set(lam.UNITS) - {"judge"}
    assert "detect" not in lam.UNITS and "one_problem" not in lam.UNITS


# ---- one run per incident window: a second problem for the same network within 15 minutes is the same incident ----------------
def test_a_second_problem_within_15_minutes_is_the_same_incident(lam):
    first = lam.handler(event("P-1"), CTX)
    second = lam.handler(event("P-2"), CTX)
    assert first["statusCode"] == 202 and second["statusCode"] == 200
    assert json.loads(second["body"])["run_id"] == json.loads(first["body"])["run_id"] and json.loads(second["body"])["duplicate"] is True
    assert lam.lam.invoke.call_count == 1


def test_after_15_minutes_a_new_incident_starts(lam, monkeypatch):
    lam.handler(event("P-1"), CTX)
    real = lam.time.time
    monkeypatch.setattr(lam.time, "time", lambda: real() + 16 * 60)
    out = lam.handler(event("P-3"), CTX)
    assert out["statusCode"] == 202 and lam.lam.invoke.call_count == 2


# ---- the pause run: a manual trigger may ask for the pause demo, and then the declaration includes its unit ----------------
def test_a_pause_request_is_declared_and_passed_on(lam):
    out = lam.handler(event("pause-demo-1", pause=True), CTX)
    decl = json.loads(lam.s3.put_object.call_args.kwargs["Body"])
    assert "swarm_pause" in decl["units"] and "swarm_pause" in decl["target"]
    sent = json.loads(lam.lam.invoke.call_args.kwargs["Payload"])["_run"]
    assert sent["pause"] is True and out["statusCode"] == 202


def test_an_ordinary_run_does_not_pause_or_declare_the_pause_unit(lam):
    lam.handler(event("P-9"), CTX)
    decl = json.loads(lam.s3.put_object.call_args.kwargs["Body"])
    assert "swarm_pause" not in decl["units"]
    assert json.loads(lam.lam.invoke.call_args.kwargs["Payload"])["_run"]["pause"] is False
