#!/bin/bash
# Removes the old log-record detector "Viavi twin: phones without service" (replaced by detectors/*.json, ./apply-detection.sh).
#   ./detector.sh delete     removes it
#   ./detector.sh status     shows whether it still exists
set -euo pipefail
cd "$(dirname "$0")"
CTX="${DT_CONTEXT:-fyuz}"
SCHEMA=builtin:davis.anomaly-detectors
TITLE="Viavi twin: phones without service"
find_id() {
  dtctl --context "$CTX" get settings --schema "$SCHEMA" -o json --no-agent --plain \
    | python3 -c 'import json,sys; t=sys.argv[1]; print("\n".join(o["objectId"] for o in json.load(sys.stdin) if o["value"].get("title")==t))' "$TITLE"
}
case "${1:-status}" in
  delete)  # removes the old log-record detector (replaced by the metric detectors, see apply-detection.sh)
    for id in $(find_id); do dtctl --context "$CTX" delete settings "$id"; done ;;
  status)
    echo "old log detector: $(find_id || true)" ;;
  *) echo "usage: $0 delete|status"; exit 2 ;;
esac
