"""The OpenPipeline pipeline for the Viavi twin: a Cell level in Smartscape, cell metrics, and no alerts raised from log fields."""

import json
from pathlib import Path

PIPELINE = json.loads((Path(__file__).resolve().parent.parent / "scenarios" / "twin" / "dynatrace" / "pipeline.json").read_text())
NODES = {p["id"]: p for p in PIPELINE["smartscapeNodeExtraction"]["processors"]}
METRICS = {p["id"]: p for p in PIPELINE["metricExtraction"]["processors"]}


def components(p):
    return {c["idComponent"]: c["referencedFieldName"] for c in p["smartscapeNode"]["idComponents"]}


def test_cell_node_processor_present():
    n = NODES["gy_twin_cell_node"]["smartscapeNode"]
    assert (n["nodeType"], n["extractNode"], n["nodeIdFieldName"]) == ("CUSTOM_CELL", True, "dt.smartscape_source.id")
    assert components(NODES["gy_twin_cell_node"]) == {"network": "twin.network", "cell": "twin.cell"}
    assert n["nodeName"]["field"]["sourceFieldName"] == "twin.cell_full"
    fields = {f["fieldName"] for f in n["fieldsToExtract"]}
    assert {"twin.criticality", "twin.state", "twin.tx_power_dbm", "twin.tilt_deg"} <= fields
    edges = {(e["edgeType"], e["targetIdFieldName"], e["targetType"]) for e in n["staticEdgesToExtract"]}
    assert ("belongs_to", "dt.smartscape.cell_tower", "CUSTOM_CELL_TOWER") in edges
    for i in (1, 2, 3):
        assert ("connects_to", f"dt.smartscape.neighbor_{i}", "CUSTOM_CELL") in edges


def test_tower_node_is_the_site():
    n = NODES["gy_twin_tower_node"]["smartscapeNode"]
    assert (n["nodeType"], n["nodeIdFieldName"]) == ("CUSTOM_CELL_TOWER", "dt.smartscape.cell_tower")
    assert components(NODES["gy_twin_tower_node"]) == {"network": "twin.network", "tower": "twin.site"}
    assert ("belongs_to", "dt.smartscape.cell_network", "CUSTOM_CELL_NETWORK") in {
        (e["edgeType"], e["targetIdFieldName"], e["targetType"]) for e in n["staticEdgesToExtract"]}


def test_neighbour_edges_are_cell_level():
    for i in (1, 2, 3):
        p = NODES[f"gy_twin_neighbor_{i}"]
        assert p["smartscapeNode"]["nodeType"] == "CUSTOM_CELL"
        assert components(p) == {"network": "twin.network", "cell": f"twin.neighbor_{i}"}


def test_agent_changes_tag_the_cell():
    assert NODES["gy_twin_change_tower"]["smartscapeNode"]["nodeType"] == "CUSTOM_CELL"


def test_metrics_carry_cell_dimension():
    per_cell = [m for m in METRICS.values() if "twin.kind, \"cell\"" in m["matcher"]]
    assert len(per_cell) >= 7
    for m in per_cell:
        dims = {d["sourceFieldName"] for d in m["valueMetric"]["dimensions"]}
        assert {"twin.cell_full", "twin.criticality", "dt.smartscape_source.id"} <= dims, m["id"]


def test_enabled_metric_present():
    m = METRICS["gy_twin_m_enabled"]["valueMetric"]
    assert (m["metricKey"], m["field"]) == ("log.viavi_twin.enabled", "twin.enabled")


def test_log_davis_processors_removed():
    assert PIPELINE["davis"]["processors"] == []


def test_every_processor_id_unique():
    ids = [p["id"] for sec in ("smartscapeNodeExtraction", "metricExtraction", "davis") for p in PIPELINE[sec]["processors"]]
    assert len(ids) == len(set(ids))
