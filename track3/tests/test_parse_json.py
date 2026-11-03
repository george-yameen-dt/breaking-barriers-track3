"""incident.parse_json takes the first JSON object in a model's text, however much else the model wrote."""

from incident import parse_json


def test_plain_object():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_text_around_one_object():
    assert parse_json('Here is the answer:\n{"a": {"b": 2}}\nDone.') == {"a": {"b": 2}}


def test_two_objects_take_the_first():
    assert parse_json('{"assessments": []} then again {"assessments": [1]}') == {"assessments": []}


def test_braces_in_prose_before_the_object():
    assert parse_json('use {curly} braces like so: {"ok": true}') == {"ok": True}


def test_no_object_is_empty():
    assert parse_json("no json here") == {}
