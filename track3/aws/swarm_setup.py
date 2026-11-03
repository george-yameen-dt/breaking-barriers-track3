"""Creates (or updates) the five swarm runtimes on AgentCore: one per agent, all from agentcore/track3_swarm.zip.

    AWS_PROFILE=fyuz ~/.local/venvs/mobile-env/bin/python aws/swarm_setup.py

For now all five share the role team04-track3-runtime; this adds the two permissions the swarm needs to that role's
inline policy (the orchestrator invoking the other runtimes, and the atomic record counter). Writes the runtime ARNs to
aws/swarm-runtimes.json, which swarm.py --cloud reads.
"""

import json
import time
from pathlib import Path

import boto3

REGION, ACCOUNT = "us-east-2", "279760855394"
ROLE_NAME, POLICY_NAME = "team04-track3-runtime", "track3-runtime"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/bb26/{ROLE_NAME}"
CODE = {"s3": {"bucket": "bb26-p-team04-data-279760855394-use2", "prefix": "agentcore/track3_swarm.zip"}}
# Same VPC as the sswitalski-EC2 VM (vpc-08554422beaa5aa27): the two private subnets (NAT egress + S3 endpoint).
NETWORK = {"networkMode": "VPC", "networkModeConfig": {
    "subnets": ["subnet-0d843f7aeb9ba52bf", "subnet-0a1323fea3a26488c"],
    "securityGroups": ["sg-01daf5b1c0c0300ae"]}}  # team04-track3-runtime-sg: no inbound, all outbound
OUT = Path(__file__).parent / "swarm-runtimes.json"
WORKERS = ("coverage", "capacity", "availability", "risk")

iam = boto3.client("iam")
ac = boto3.client("bedrock-agentcore-control", region_name=REGION)


def add_permissions() -> None:
    doc = iam.get_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME)["PolicyDocument"]
    sids = {s.get("Sid") for s in doc["Statement"]}
    extra = [
        {"Sid": "SwarmInvokeAgents", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
         "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/track3_*"},
        {"Sid": "SwarmRecordCounter", "Effect": "Allow", "Action": "dynamodb:UpdateItem",
         "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/bb26-team04-track3-records"},
    ]
    new = [s for s in extra if s["Sid"] not in sids]
    if new:
        doc["Statement"] += new
        iam.put_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(doc))
        print(f"role {ROLE_NAME}: added {', '.join(s['Sid'] for s in new)}")


def existing() -> dict:
    return {r["agentRuntimeName"]: r for r in ac.list_agent_runtimes()["agentRuntimes"]}


def deploy(role: str, env: dict) -> str:
    name = f"track3_{role}"
    args = dict(agentRuntimeArtifact={"codeConfiguration": {"code": CODE, "runtime": "PYTHON_3_12", "entryPoint": ["opentelemetry-instrument", "main.py"]}},
                roleArn=ROLE_ARN, networkConfiguration=NETWORK, protocolConfiguration={"serverProtocol": "HTTP"},
                environmentVariables={"AGENT_ROLE": role, "OTEL_SERVICE_NAME": "multi-agent-swarm", "VIAVI_RSG_URL": "http://10.0.1.59/mcp", **env},
                lifecycleConfiguration={"idleRuntimeSessionTimeout": 900, "maxLifetime": 3600},
                description=f"Track 3 swarm: the {role} agent")
    have = existing().get(name)
    if have:
        r = ac.update_agent_runtime(agentRuntimeId=have["agentRuntimeId"], **args)
    else:
        r = ac.create_agent_runtime(agentRuntimeName=name, **args)
    return r["agentRuntimeId"]


def wait_ready(ids: dict) -> dict:
    arns = {}
    while len(arns) < len(ids):
        for role, rid in ids.items():
            r = ac.get_agent_runtime(agentRuntimeId=rid)
            if r["status"] == "READY":
                arns[role] = r["agentRuntimeArn"]
            elif "FAILED" in r["status"]:
                raise SystemExit(f"{role}: {r['status']} {r.get('failureReason', '')}")
        time.sleep(5)
    return arns


def main() -> None:
    add_permissions()
    time.sleep(10)  # IAM changes take a few seconds to reach AgentCore
    arns = wait_ready({role: deploy(role, {}) for role in WORKERS})
    print("ready:", ", ".join(arns))
    env = {f"{role.upper()}_RUNTIME_ARN": arn for role, arn in arns.items()}
    arns["orchestrator"] = wait_ready({"orchestrator": deploy("orchestrator", env)})["orchestrator"]
    OUT.write_text(json.dumps(arns, indent=1) + "\n")
    for role, arn in arns.items():
        print(f"{role:13} {arn}")


if __name__ == "__main__":
    main()
