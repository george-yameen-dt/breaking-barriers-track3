#!/usr/bin/env bash
# Creates or updates the Dynatrace workflow, filling in the Lambda URL and token from aws/trigger-config.json
# (made by aws/trigger_setup.py, gitignored). Usage: workflows/apply.sh [workflow-id]   (id: update instead of create)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cfg="$here/../aws/trigger-config.json"
url=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['url'])" "$cfg")
token=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['token'])" "$cfg")
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
sed -e "s|__TRIGGER_URL__|$url|" -e "s|__TRACK3_TOKEN__|$token|" "$here/track3-problem-trigger.yaml" > "$tmp"
dtctl apply -f "$tmp" ${1:+--id "$1"}
