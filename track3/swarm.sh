#!/bin/bash
# Runs the five-agent swarm (swarm.py) with the team AWS profile. Dev runs: no Dynatrace or Arize mirror of the records.
set -e
cd "$(dirname "$0")"
source ~/fyuz.env >/dev/null
exec ~/.local/venvs/mobile-env/bin/python swarm.py "$@"
