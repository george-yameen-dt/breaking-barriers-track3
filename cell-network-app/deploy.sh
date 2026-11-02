#!/bin/bash
# Bumps the patch version in app.config.json (AppEngine rejects re-deploying the same version), then deploys.
set -e
cd "$(dirname "$0")"
python3 - <<'PY'
import json
p = "app.config.json"
d = json.load(open(p))
major, minor, patch = d["app"]["version"].split(".")
d["app"]["version"] = f"{major}.{minor}.{int(patch) + 1}"
json.dump(d, open(p, "w"), indent=2)
open(p, "a").write("\n")
print("version", d["app"]["version"])
PY
export PATH="$HOME/.local/node/bin:$PATH"
npx dt-app deploy 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -E "App is deployed|Error|HTTP|Cannot|✖" || true
