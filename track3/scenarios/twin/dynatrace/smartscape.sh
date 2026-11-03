#!/bin/bash
# The twin on Dynatrace's map: OpenPipeline pipeline "Viavi twin cells (team04)" (pipeline.json, customId
# gy-viavi-twin-cells) plus the routing entry for service.name == "viavi-twin". It draws the network, the towers (sites)
# and their cells in Smartscape and extracts the cell metrics; Davis anomaly detectors on those metrics
# (./apply-detection.sh) raise the problems. It replaces the teammate's gy-viavi-twin pipeline, which this account
# cannot edit (403: not shared): apply points the existing routing entry at the new pipeline, undo points it back.
#   ./smartscape.sh validate   the API checks pipeline.json; nothing is created
#   ./smartscape.sh apply      creates (or updates) the pipeline, points the routing entry at it (others kept), removes the old log detector
#   ./smartscape.sh undo       points the routing entry back at the pipeline it had (routing.before-cells.json) and deletes this pipeline
#   ./smartscape.sh status     what exists now
set -euo pipefail
cd "$(dirname "$0")"
CTX="${DT_CONTEXT:-fyuz}"
CUSTOM_ID=gy-viavi-twin-cells
ENTRY="Viavi twin (team04)"
P_SCHEMA=builtin:openpipeline.logs.pipelines
R_SCHEMA=builtin:openpipeline.logs.routing
dt() { dtctl --context "$CTX" "$@"; }
pipeline_id() {
  dt get settings --schema "$P_SCHEMA" -o json --no-agent --plain \
    | python3 -c 'import json,sys; print("\n".join(o["objectId"] for o in json.load(sys.stdin) if o["value"].get("customId")==sys.argv[1]))' "$CUSTOM_ID"
}
next_json() {  # pipeline.next.json: pipeline.json wrapped as an update of the existing pipeline object
  python3 - "$(pipeline_id)" "$P_SCHEMA" <<'PY'
import json, sys
json.dump({"objectId": sys.argv[1], "schemaId": sys.argv[2], "scope": "environment", "value": json.load(open("pipeline.json"))},
          open("pipeline.next.json", "w"), indent=1)
PY
}
routing() {  # $1 = point <pipeline id> | back: writes routing.next.json from the CURRENT routing; every other entry is untouched
  dt get settings --schema "$R_SCHEMA" -o json --no-agent --plain > routing.now.json
  python3 - "$@" <<'PY'
import json, sys
ENTRY = "Viavi twin (team04)"
obj = json.load(open("routing.now.json"))[0]
entries = obj["value"]["routingEntries"]
mine = next((e for e in entries if e.get("description") == ENTRY), None)
if sys.argv[1] == "point":
    if mine is None:
        mine = {"description": ENTRY, "enabled": True, "matcher": 'matchesValue(service.name, "viavi-twin")', "pipelineType": "custom"}
        entries.append(mine)
    mine["pipelineId"] = sys.argv[2]
else:  # back: the pipeline id the entry had before this pipeline (routing.before-cells.json)
    before = next(e for e in json.load(open("routing.before-cells.json"))[0]["value"]["routingEntries"] if e.get("description") == ENTRY)
    mine["pipelineId"] = before["pipelineId"]
json.dump({k: obj[k] for k in ("objectId", "schemaId", "scope", "value")}, open("routing.next.json", "w"), indent=1)
print("routing entries:", [(e["description"], e.get("pipelineId", "")[-8:]) for e in entries])
PY
}
case "${1:-validate}" in
  validate)
    if [ -z "$(pipeline_id)" ]; then dt create settings -f pipeline.json --schema "$P_SCHEMA" --scope environment --validate-only
    else next_json; dt apply -f pipeline.next.json --dry-run; fi ;;
  apply)
    [ -f routing.before-cells.json ] || dt get settings --schema "$R_SCHEMA" -o json --no-agent --plain > routing.before-cells.json
    if [ -z "$(pipeline_id)" ]; then dt create settings -f pipeline.json --schema "$P_SCHEMA" --scope environment
    else next_json; dt apply -f pipeline.next.json; fi
    routing point "$(pipeline_id)"
    dt apply -f routing.next.json
    ./detector.sh delete ;;
  undo)
    routing back
    dt apply -f routing.next.json
    for id in $(pipeline_id); do dt delete settings "$id"; done ;;
  status)
    echo "pipeline: $(pipeline_id || true)"
    dt get settings --schema "$R_SCHEMA" -o json --no-agent --plain \
      | python3 -c 'import json,sys; print("routing:", [(e["description"], e.get("pipelineId","")[-8:]) for e in json.load(sys.stdin)[0]["value"]["routingEntries"]])'
    ./detector.sh status ;;
  *) echo "usage: $0 validate|apply|undo|status"; exit 2 ;;
esac
