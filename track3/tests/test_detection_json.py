"""Metric anomaly detectors (Davis) on the cell metrics, and the event correlation rule that makes one fault one problem."""

import json
import re
from pathlib import Path

import pytest

from adapters.twin import SCENARIOS, snapshot as twin_snapshot

DT = SCENARIOS / "dynatrace"
DETECTORS = {p.stem: json.loads(p.read_text()) for p in sorted((DT / "detectors").glob("*.json"))}
PIPELINE = json.loads((DT / "pipeline.json").read_text())
METRIC_KEYS = {m["valueMetric"]["metricKey"] for m in PIPELINE["metricExtraction"]["processors"]}
THRESHOLDS = {"qos": ("BELOW", 90), "phones": ("ABOVE", 4), "rsrp": ("BELOW", -125), "dark": ("BELOW", 1)}


def inputs(d):
    return {i["key"]: i["value"] for i in d["analyzer"]["input"]}


def test_the_four_detectors_exist():
    assert set(DETECTORS) == set(THRESHOLDS)


@pytest.mark.parametrize("name", sorted(THRESHOLDS))
def test_each_detector_reads_a_cell_metric_per_cell(name):
    d, query = DETECTORS[name], inputs(DETECTORS[name])["query.expression"]
    assert d["analyzer"]["name"].endswith("StaticThresholdAnomalyDetectionAnalyzer")
    assert query.startswith("timeseries") and "fetch" not in query, "a detector reads a metric, never the logs"
    assert "dt.smartscape_source.id" in re.search(r"by:\s*\{([^}]*)\}", query).group(1)
    keys = set(re.findall(r"log\.viavi_twin\.[a-z0-9_.]+", query))
    assert keys and keys <= METRIC_KEYS, keys - METRIC_KEYS


@pytest.mark.parametrize("name,expect", sorted(THRESHOLDS.items()))
def test_thresholds_are_the_declared_ones(name, expect):
    i = inputs(DETECTORS[name])
    assert (i["alertCondition"], float(i["threshold"])) == (expect[0], expect[1])


@pytest.mark.parametrize("name", sorted(THRESHOLDS))
def test_events_land_on_the_cell_and_share_one_alert_group(name):
    props = {p["key"]: p["value"] for p in DETECTORS[name]["eventTemplate"]["properties"]}
    assert props["dt.smartscape_source.id"] == "{dims:dt.smartscape_source.id}"
    assert props["dt.alert_group"] == "viavi-twin"


def test_phones_threshold_stays_quiet_before_the_outage():
    """The saved incident: more than 4 phones without service near a cell from the outage window on, never before it
    (after the saved run's own fix at 360 s the worst cell still has more than 4, which is true: it is not yet healthy)."""
    import copy
    from test_twin import SPEC
    data = json.loads((SCENARIOS / SPEC["recording"]).read_text())
    cfg = {c: {"enabled": 1, "tx_power_dbm": 40, "tilt_deg": 6, "classification": "production", "maintenance_window": 0}
           for c in ("S1", "S2", "S3", "S4")}
    threshold = float(inputs(DETECTORS["phones"])["threshold"])
    firing = [tuple(w["t"]) for w in data["windows"]
              if max(c["dropped_nearby"] for c in twin_snapshot(copy.deepcopy(w), cfg, 39.0)["cells"]) > threshold]
    assert firing and firing[0] == (300, 360) and all(t[0] >= 300 for t in firing), firing


def test_the_old_log_detector_is_gone():
    assert not (DT / "detector.json").exists()


def test_correlation_rule_groups_by_network():
    rule = json.loads((DT / "correlation-rules.json").read_text())
    assert rule["displayName"] == "Viavi cell anomalies per network" and rule["enabled"] is True
    assert rule["groupingFields"] == ["twin.network_name"] and rule["timeWindow"] == "5m"
    assert "viavi-twin" in rule["matchingCondition"]
