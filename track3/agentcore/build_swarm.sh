#!/bin/bash
# Packages the five-agent swarm for AgentCore Runtime direct code deployment (Linux arm64, Python 3.12) and uploads
# s3://$BUCKET/agentcore/track3_swarm.zip. All five runtimes run this zip; AGENT_ROLE says which agent each one is.
set -e
cd "$(dirname "$0")"
BUCKET=bb26-p-team04-data-279760855394-use2
rm -rf build_swarm track3_swarm.zip && mkdir -p build_swarm/policies build_swarm/scenarios
~/.local/bin/uv pip install --python-platform aarch64-manylinux2014 --python-version 3.12 --target build_swarm --only-binary=:all: -r requirements.txt -q
cp main.py ../agent_loop.py ../checkpoint.py ../policy.py ../telemetry.py ../gateway.py ../incident.py ../contracts.py ../rfmodel.py ../risk_checks.py ../dt_tools.py build_swarm/
cp -R ../agents ../adapters build_swarm/
cp ../policies/track3.cedar ../policies/swarm.cedar ../policies/promotion.cedar build_swarm/policies/
cp -R ../scenarios/faults ../scenarios/swarm-layout.json build_swarm/scenarios/
find build_swarm -name __pycache__ -prune -exec rm -rf {} +
find build_swarm -type d -exec chmod 755 {} + && find build_swarm -type f -exec chmod 644 {} +
(cd build_swarm && zip -qr ../track3_swarm.zip .)
ls -lh track3_swarm.zip | awk '{print "zip", $5}'
AWS_PROFILE=${AWS_PROFILE:-bb26} aws s3 cp track3_swarm.zip "s3://$BUCKET/agentcore/track3_swarm.zip" --region us-east-2 --only-show-errors
echo "uploaded s3://$BUCKET/agentcore/track3_swarm.zip"
