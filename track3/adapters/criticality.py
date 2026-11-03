"""Which cells are critical (a hospital, an emergency dispatch centre, military): metadata we own, not Viavi's.

The labels are synthetic and say so in the registry file. A cell not in the registry is "none", never an error, so a
network with hundreds of cells needs no entry for the ordinary ones.
"""

import json
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "scenarios" / "twin" / "critical-cells.json"
CLASSES = ("hospital", "emergency", "military")


def load(path: Path = DEFAULT) -> dict:
    """The registry: Viavi cell name -> {"class": ..., "name": ...}. Keys starting with _ are notes."""
    return {k: v for k, v in json.loads(Path(path).read_text()).items() if not k.startswith("_")}


def _site(cell_id) -> str:
    return str(cell_id or "").split("/")[0].strip().upper()


def criticality_of(cell_id, registry: dict | None = None) -> str:
    """"hospital", "emergency", "military" or "none" for 'S2' or 'S2/B3/C1'. Unknown cells are "none"."""
    reg = load() if registry is None else registry
    for name, entry in reg.items():
        if _site(name) == _site(cell_id) and entry.get("class") in CLASSES:
            return entry["class"]
    return "none"
