#!/bin/bash
# Cell network demo: mobile-env window + every step streamed to yqs73977.
#   ./demo.sh            window, starts on v1, type 1 or 2 + Enter to switch
#   ./demo.sh --auto 60  window, flips v1/v2 every 60 s (hands-free)
#   ./demo.sh --headless --episodes 2   no window, just send data
# Needs GlobalProtect up and DT_PLATFORM_TOKEN in ~/.bindplane/.env (kept off OneDrive).
set -e
cd "$(dirname "$0")"
set -a; . ~/.bindplane/.env; set +a
exec ~/.local/venvs/mobile-env/bin/python sim.py "$@"
