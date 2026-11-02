#!/bin/bash
# Runs track3.py with the team AWS profile and the Dynatrace token for the operator view.
set -e
cd "$(dirname "$0")"
source ~/fyuz.env >/dev/null
set -a; . ~/.bindplane/.env; set +a
exec ~/.local/venvs/mobile-env/bin/python track3.py "$@"
