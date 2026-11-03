#!/bin/bash
# The twin's problem card: one Davis anomaly detector on yqs73977 (dtctl context astroshop), from detector.json.
#   ./detector.sh validate   the API checks detector.json; nothing is created
#   ./detector.sh create     creates the detector (a change on the tenant)
#   ./detector.sh delete     removes it again (the undo)
#   ./detector.sh status     lists it, and the twin's open problems
set -euo pipefail
cd "$(dirname "$0")"
CTX=astroshop
SCHEMA=builtin:davis.anomaly-detectors
TITLE="Viavi twin: phones without service"
find_id() {
  dtctl --context "$CTX" get settings --schema "$SCHEMA" -o json --no-agent --plain \
    | python3 -c 'import json,sys; t=sys.argv[1]; print("\n".join(o["objectId"] for o in json.load(sys.stdin) if o["value"].get("title")==t))' "$TITLE"
}
case "${1:-validate}" in
  validate) dtctl --context "$CTX" create settings -f detector.json --schema "$SCHEMA" --scope environment --validate-only ;;
  create)
    [ -n "$(find_id)" ] && { echo "already there: $(find_id)"; exit 0; }
    dtctl --context "$CTX" create settings -f detector.json --schema "$SCHEMA" --scope environment ;;
  delete)
    for id in $(find_id); do dtctl --context "$CTX" delete settings "$id"; done ;;
  status)
    echo "detector: $(find_id || true)"
    dtctl --context "$CTX" query 'fetch dt.davis.problems, from:now()-2h | filter event.status == "ACTIVE" and matchesPhrase(event.name, "Viavi twin") | fields display_id, event.name, event.start' --no-agent --plain ;;
  *) echo "usage: $0 validate|create|delete|status"; exit 2 ;;
esac
