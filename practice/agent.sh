#!/bin/bash
# Cell network ops agent: watches for the safety problem on yqs73977 and asks Claude what to do.
#   ./agent.sh          advise only
#   ./agent.sh --act    also roll the simulator back to v1 when Claude recommends it
# Needs GlobalProtect, dtctl (current context on tenant yqs73977), the claude CLI, DT_PLATFORM_TOKEN in ~/.bindplane/.env.
set -e
cd "$(dirname "$0")"
set -a; . ~/.bindplane/.env; set +a
exec ~/.local/venvs/mobile-env/bin/python agent.py "$@"
