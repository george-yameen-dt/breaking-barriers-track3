#!/bin/bash
# One command for the whole demo: the AI ops agent in the background, the simulator window in front.
# The simulator plays the 3-step story on a loop: ① healthy (v1) → ② broken (v2, Dynatrace opens a problem)
# → ③ the agent asks Claude and rolls back to v1. Close the window or press Ctrl-C to stop both.
# Needs GlobalProtect up.
set -e
cd "$(dirname "$0")"
echo "cell-ops-agent · checking Dynatrace every 15 s (log: agent.log)" > agent.log
# The agent runs as a fresh check every 15 s; it remembers handled problems in ~/.local/state/cell-ops-agent.
(while true; do ./agent.sh --act --once >> agent.log 2>&1; sleep 15; done) &
AGENT=$!
trap 'kill $AGENT 2>/dev/null; pkill -f "agent.py --act --once" 2>/dev/null' EXIT
echo "ops agent running (log: agent.log)"
./demo.sh --story "$@"
