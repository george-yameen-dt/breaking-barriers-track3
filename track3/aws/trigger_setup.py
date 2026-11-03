"""Creates (or updates) the Lambda the Dynatrace workflow calls: function URL -> orchestrator AgentCore runtime.

    AWS_PROFILE=fyuz python aws/trigger_setup.py

Reads the orchestrator ARN from aws/swarm-runtimes.json. The shared secret is made once and kept in
aws/trigger-config.json (gitignored); the workflow sends it as the x-track3-token header.
"""

import io
import json
import secrets
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION, ACCOUNT = "us-east-2", "279760855394"
NAME, ROLE_NAME = "track3-trigger", "team04-track3-trigger"
BOUNDARY = f"arn:aws:iam::{ACCOUNT}:policy/bb26-boundary/bb26-Boundary"
HERE = Path(__file__).parent
CONFIG = HERE / "trigger-config.json"

iam, lam = boto3.client("iam"), boto3.client("lambda", region_name=REGION)


def ensure_role(orchestrator_arn: str) -> str:
    try:
        iam.create_role(RoleName=ROLE_NAME, Path="/bb26/", AssumeRolePolicyDocument=(HERE / "trust-lambda.json").read_text(),
                        PermissionsBoundary=BOUNDARY)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
    iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="track3-trigger", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime", "Resource": [orchestrator_arn, orchestrator_arn + "/*"]},
            {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{NAME}"},
            {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:GetItem"],
             "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/bb26-team04-track3-records"}]}))
    return f"arn:aws:iam::{ACCOUNT}:role/bb26/{ROLE_NAME}"


def build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "trigger_lambda.py", "trigger_lambda.py")
        z.write(HERE.parent / "adapters" / "__init__.py", "adapters/__init__.py")
        z.write(HERE.parent / "adapters" / "criticality.py", "adapters/criticality.py")
        z.write(HERE.parent / "scenarios" / "twin" / "critical-cells.json", "scenarios/twin/critical-cells.json")
    return buf.getvalue()


def main() -> None:
    orchestrator = json.loads((HERE / "swarm-runtimes.json").read_text())["orchestrator"]
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {"token": secrets.token_urlsafe(32)}
    role_arn = ensure_role(orchestrator)
    time.sleep(10)  # a new role takes a few seconds to be usable by Lambda
    env = {"Variables": {"ORCHESTRATOR_ARN": orchestrator, "TRIGGER_TOKEN": cfg["token"]}}
    try:
        lam.get_function(FunctionName=NAME)
        lam.update_function_code(FunctionName=NAME, ZipFile=build_zip())
        lam.get_waiter("function_updated").wait(FunctionName=NAME)
        lam.update_function_configuration(FunctionName=NAME, Environment=env, Timeout=900)
    except lam.exceptions.ResourceNotFoundException:
        lam.create_function(FunctionName=NAME, Runtime="python3.12", Role=role_arn, Handler="trigger_lambda.handler",
                            Code={"ZipFile": build_zip()}, Timeout=900, MemorySize=256, Environment=env,
                            Description="Dynatrace workflow -> Track 3 orchestrator (fire and forget)")
    lam.get_waiter("function_active_v2").wait(FunctionName=NAME)
    lam.put_function_event_invoke_config(FunctionName=NAME, MaximumRetryAttempts=0)  # a failed run must not start twice
    try:
        url = lam.create_function_url_config(FunctionName=NAME, AuthType="NONE")["FunctionUrl"]
    except lam.exceptions.ResourceConflictException:
        url = lam.get_function_url_config(FunctionName=NAME)["FunctionUrl"]
    for sid, action, extra in (("public-url", "lambda:InvokeFunctionUrl", {"FunctionUrlAuthType": "NONE"}),
                               ("public-invoke", "lambda:InvokeFunction", {"InvokedViaFunctionUrl": True})):
        try:
            lam.add_permission(FunctionName=NAME, StatementId=sid, Action=action, Principal="*", **extra)
        except lam.exceptions.ResourceConflictException:
            pass
    cfg["url"] = url
    CONFIG.write_text(json.dumps(cfg, indent=1) + "\n")
    print(f"function URL {url}\ntoken saved in {CONFIG.name} (not printed)")


if __name__ == "__main__":
    main()
