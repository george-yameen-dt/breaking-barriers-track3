#!/bin/bash
# The twin on Dynatrace's map: OpenPipeline pipeline "Viavi twin (team04)" (pipeline.json) plus one routing entry for
# service.name == "viavi-twin". It draws the network, the towers (sites) and their cells in Smartscape and extracts the cell metrics;
# Davis anomaly detectors on those metrics (detectors/) raise the problems. It replaces the earlier log-field detector.
#   ./smartscape.sh validate   the API checks pipeline.json; nothing is created
#   ./smartscape.sh apply      creates the pipeline, adds the routing entry (others kept), removes the detector
#   ./smartscape.sh undo       removes the routing entry and the pipeline (routing.before-twin.json is the backup)
#   ./smartscape.sh status     what exists now
set -euo pipefail
cd "$(dirname "$0")"
CTX="${DT_CONTEXT:-fyuz}"
P_SCHEMA=builtin:openpipeline.logs.pipelines
R_SCHEMA=builtin:openpipeline.logs.routing
dt() { dtctl --context "$CTX" "$@"; }
pipeline_id() {  # the settings list does not always show this pipeline, so fall back to the pipeline id its routing entry names
  local id
  id=$(dt get settings --schema "$P_SCHEMA" -o json --no-agent --plain \
    | python3 -c 'import json,sys; print("\n".join(o["objectId"] for o in json.load(sys.stdin) if o["value"].get("customId")=="gy-viavi-twin"))')
  if [ -z "$id" ]; then
    id=$(dt get settings --schema "$R_SCHEMA" -o json --no-agent --plain \
      | python3 -c 'import json,sys; print("\n".join(e["pipelineId"] for e in json.load(sys.stdin)[0]["value"]["routingEntries"] if e.get("description")=="Viavi twin (team04)"))')
  fi
  echo "$id"
}
next_json() {  # pipeline.next.json: pipeline.json wrapped as an update of the existing pipeline object
  python3 - "$(pipeline_id)" "$P_SCHEMA" <<'PY'
import json, sys
json.dump({"objectId": sys.argv[1], "schemaId": sys.argv[2], "scope": "environment", "value": json.load(open("pipeline.json"))},
          open("pipeline.next.json", "w"), indent=1)
PY
}
routing() {  # $1 = add <pipeline id> | remove: writes routing.next.json from the CURRENT routing, other entries untouched
  dt get settings --schema "$R_SCHEMA" -o json --no-agent --plain > routing.now.json
  python3 - "$@" <<'PY'
import json, sys
obj = json.load(open("routing.now.json"))[0]
entries = [e for e in obj["value"]["routingEntries"] if e.get("description") != "Viavi twin (team04)"]
if sys.argv[1] == "add":
    entries.append({"description": "Viavi twin (team04)", "enabled": True, "matcher": "matchesValue(service.name, \"viavi-twin\")",
                    "pipelineId": sys.argv[2], "pipelineType": "custom"})
obj["value"]["routingEntries"] = entries
json.dump({k: obj[k] for k in ("objectId", "schemaId", "scope", "value")}, open("routing.next.json", "w"), indent=1)
print("routing entries:", [e["description"] for e in entries])
PY
}
case "${1:-validate}" in
  validate)  # the API checks pipeline.json; an existing pipeline is checked as an update (dry run)
    if [ -z "$(pipeline_id)" ]; then
      dt create settings -f pipeline.json --schema "$P_SCHEMA" --scope environment --validate-only
    else
      next_json; dt apply -f pipeline.next.json --dry-run
    fi ;;
  plan)  # what apply would do to the routing; changes nothing
    routing add "<the new pipeline's id>"
    dt apply -f routing.next.json --dry-run ;;
  apply)  # creates the pipeline, or updates it in place when it already exists (a changed pipeline.json)
    if [ -z "$(pipeline_id)" ]; then
      dt create settings -f pipeline.json --schema "$P_SCHEMA" --scope environment
    else
      next_json
      dt apply -f pipeline.next.json
    fi
    routing add "$(pipeline_id)"
    dt apply -f routing.next.json
    ./detector.sh delete ;;
  undo)
    routing remove
    dt apply -f routing.next.json
    for id in $(pipeline_id); do dt delete settings "$id"; done ;;
  status)
    echo "pipeline: $(pipeline_id || true)"
    dt get settings --schema "$R_SCHEMA" -o json --no-agent --plain \
      | python3 -c 'import json,sys; print("routing:", [e["description"] for e in json.load(sys.stdin)[0]["value"]["routingEntries"]])'
    ./detector.sh status ;;
  *) echo "usage: $0 validate|apply|undo|status"; exit 2 ;;
esac
