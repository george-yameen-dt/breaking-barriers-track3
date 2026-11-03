#!/usr/bin/env bash
# Sync this checkout's track3/ (git-tracked files, committed) to the team04 EC2, for viavi-run and viavi-reset. Run on a laptop.
#
#   track3/scenarios/twin/deploy-ec2.sh [--setup]
#
# --setup, once per box: installs python3.11 (dnf) and the venv ~/team04-track3/.venv with boto3.
# Refuses while track3/ has uncommitted changes, so the EC2 always runs a known commit (~/team04-track3/REVISION).
# Copies only, never deletes: dev-runs/ on the box stays. EC2_HOST overrides the ssh host (default hackathon-ec2).
set -euo pipefail
HOST="${EC2_HOST:-hackathon-ec2}"
DEST="team04-track3"
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain -- track3)" ]; then
  echo "track3/ has uncommitted changes: commit them first, so the EC2 runs a known commit" >&2
  exit 1
fi
REV="$(git rev-parse --short HEAD) $(git branch --show-current)"
git ls-files -z track3 | rsync -a --from0 --files-from=- ./ "$HOST:$DEST/"
ssh "$HOST" "install -m 755 $DEST/track3/scenarios/twin/viavi-run $DEST/viavi-run && install -m 755 $DEST/track3/scenarios/twin/viavi-reset $DEST/viavi-reset && echo '$REV' > $DEST/REVISION"
if [ "${1:-}" = "--setup" ]; then
  ssh "$HOST" "set -e
    command -v python3.11 >/dev/null || sudo dnf install -y -q python3.11
    [ -x $DEST/.venv/bin/python ] || python3.11 -m venv $DEST/.venv
    $DEST/.venv/bin/pip install -q --upgrade pip boto3
    $DEST/.venv/bin/python --version"
fi
echo "deployed $REV to $HOST:~/$DEST"
