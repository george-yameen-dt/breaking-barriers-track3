#!/bin/bash
# Packages the Track 3 agents for AgentCore Runtime direct code deployment (Linux arm64, Python 3.12)
# and uploads the zip to s3://$BUCKET/agentcore/track3_agents.zip.
set -e
cd "$(dirname "$0")"
BUCKET=bb26-p-team04-data-279760855394-use2
rm -rf build track3_agents.zip && mkdir -p build/policies
~/.local/bin/uv pip install --python-platform aarch64-manylinux2014 --python-version 3.12 --target build --only-binary=:all: -r requirements.txt -q
cp main.py ../checkpoint.py ../policy.py ../gateway.py ../incident.py build/
cp ../policies/track3.cedar ../policies/promotion.cedar build/policies/
find build -name __pycache__ -prune -exec rm -rf {} +
find build -type d -exec chmod 755 {} + && find build -type f -exec chmod 644 {} +
(cd build && zip -qr ../track3_agents.zip .)
ls -lh track3_agents.zip | awk '{print "zip", $5}'
AWS_PROFILE=${AWS_PROFILE:-bb26} aws s3 cp track3_agents.zip "s3://$BUCKET/agentcore/track3_agents.zip" --region us-east-2 --only-show-errors
echo "uploaded s3://$BUCKET/agentcore/track3_agents.zip"
