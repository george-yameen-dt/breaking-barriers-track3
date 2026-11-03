"""The Dynatrace workflow's Lambda: one run per problem, a stable trace id, and an alert text that names the cells."""

import importlib.util
import json
import os
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

    def put_item(TableName, Item, ConditionExpression=None):
        key = (Item["run_id"]["S"], Item["seq"]["S"])
        if key in store:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")
        store[key] = Item

    def get_item(TableName, Key, ConsistentRead=True):
        return {"Item": store[(Key["run_id"]["S"], Key["seq"]["S"])]}

    mod.ddb = SimpleNamespace(put_item=put_item, get_item=get_item)
    mod.lam = MagicMock()
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


def test_a_different_problem_starts_its_own_run(lam):
    lam.handler(event("P-1"), CTX)
    lam.handler(event("P-2"), CTX)
    assert lam.lam.invoke.call_count == 2


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
