"""AgentCore Runtime entry point for the Track 3 swarm: one runtime per agent, all from track3_swarm.zip.

AGENT_ROLE says which agent a runtime is. The payload's mode picks the part it plays (agents/cloud.py):
  "swarm"       the orchestrator runs the incident and calls the other four runtimes
  "specialist"  coverage, capacity or availability investigates and proposes
  "critic"      the risk agent assesses the proposals
The checkpoint runs here, next to the agents. Every record goes to CloudWatch Logs, DynamoDB, Dynatrace and Arize under
the run's trace id. Tools are called through the AgentCore Gateway, where AWS's own Cedar (ENFORCE) decides again.
"""

import json
import os

import boto3

REGION = "us-east-2"
MODES = ("swarm", "specialist", "critic")


def load_secrets() -> None:
    """Dynatrace logs token and Arize key for the operator-view mirrors; the official record does not need them."""
    try:
        s = json.loads(boto3.client("secretsmanager", region_name=REGION).get_secret_value(SecretId="team04/track3")["SecretString"])
        for k, v in s.items():
            os.environ.setdefault(k, v)
    except Exception as e:  # the official record (CloudWatch, DynamoDB, S3) must not depend on the mirrors
        print(f"secrets not loaded: {e}")


load_secrets()

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

from agents import cloud  # noqa: E402

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict) -> dict:
    mode = payload.get("mode")
    if mode not in MODES:
        return {"error": f"unknown mode {mode!r}; this runtime serves {', '.join(MODES)}"}
    return cloud.handle(payload)


if __name__ == "__main__":
    app.run()
