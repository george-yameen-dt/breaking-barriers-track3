#!/bin/bash
# Davis anomaly detection on the cell metrics (detectors/*.json) and the event correlation rule (correlation-rules.json).
#   ./apply-detection.sh validate   the API checks every file; nothing is created
#   ./apply-detection.sh apply      creates what is missing (by title / display name); existing ones are left alone
#   ./apply-detection.sh undo       deletes them again
#   ./apply-detection.sh status     what exists now, and the twin's open problems
# The detector runs as the person who applies it: the actor is read from `dtctl auth whoami`, not stored in the repo.
set -euo pipefail
cd "$(dirname "$0")"
CTX="${DT_CONTEXT:-fyuz}"
D=builtin:davis.anomaly-detectors
R=builtin:event-correlation-rules
dt() { dtctl --context "$CTX" "$@"; }
actor() { dt auth whoami -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["userId"])'; }
with_actor() {  # $1 = detector file -> a temp file with executionSettings.actor set
  python3 - "$1" "$(actor)" <<'PY'
import json, sys, tempfile
d = json.load(open(sys.argv[1])); d["executionSettings"] = {"actor": sys.argv[2]}
f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(d, f); print(f.name)
PY
}
ids_by() {  # $1 = schema, $2 = field (title|displayName), $3 = value
  dt get settings --schema "$1" -o json --no-agent --plain \
    | python3 -c 'import json,sys; f,v=sys.argv[1:3]; print("\n".join(o["objectId"] for o in json.load(sys.stdin) if o["value"].get(f)==v))' "$2" "$3"
}
title() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["title"])' "$1"; }
rule_name() { python3 -c 'import json; print(json.load(open("correlation-rules.json"))["displayName"])'; }
case "${1:-validate}" in
  validate)
    for f in detectors/*.json; do t=$(with_actor "$f"); dt create settings -f "$t" --schema "$D" --scope environment --validate-only; done
    dt create settings -f correlation-rules.json --schema "$R" --scope environment --validate-only ;;
  apply)
    for f in detectors/*.json; do
      [ -n "$(ids_by "$D" title "$(title "$f")")" ] && { echo "already there: $(title "$f")"; continue; }
      t=$(with_actor "$f"); dt create settings -f "$t" --schema "$D" --scope environment
    done
    [ -n "$(ids_by "$R" displayName "$(rule_name)")" ] && echo "already there: $(rule_name)" \
      || dt create settings -f correlation-rules.json --schema "$R" --scope environment ;;
  undo)
    for f in detectors/*.json; do for id in $(ids_by "$D" title "$(title "$f")"); do dt delete settings "$id"; done; done
    for id in $(ids_by "$R" displayName "$(rule_name)"); do dt delete settings "$id"; done ;;
  status)
    for f in detectors/*.json; do echo "detector $(title "$f"): $(ids_by "$D" title "$(title "$f")" | head -c 30)"; done
    echo "rule $(rule_name): $(ids_by "$R" displayName "$(rule_name)" | head -c 30)"
    dt query 'fetch dt.davis.problems, from:now()-2h | filter matchesPhrase(event.name, "Viavi twin") | fields display_id, event.name, event.status, event.start' --no-agent --plain ;;
  *) echo "usage: $0 validate|apply|undo|status"; exit 2 ;;
esac
