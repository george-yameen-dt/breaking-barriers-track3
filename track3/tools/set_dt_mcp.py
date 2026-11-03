"""Store the Dynatrace MCP endpoint and token in the team04/track3 secret (the runtimes read them at start), then test them.

    python3 tools/set_dt_mcp.py https://<tenant>.apps.dynatrace.com/platform-reserved/mcp-gateway/v0.1/servers/dynatrace-mcp/mcp

The token is typed at a hidden prompt: it is not an argument, so it stays out of shell history and the repo.
"""
import getpass
import json
import os
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dt_tools  # noqa: E402

SECRET, REGION = "team04/track3", "us-east-2"


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        print(__doc__)
        return 2
    url, token = sys.argv[1], getpass.getpass("Dynatrace MCP token (hidden): ").strip()
    os.environ["DT_MCP_URL"], os.environ["DT_MCP_TOKEN"] = url, token
    probe = dt_tools.query("fetch dt.davis.problems, from: now()-60m | limit 1")
    if "error" in probe:
        print(f"not stored: the endpoint did not answer a query: {probe['error']}")
        return 1
    sm = boto3.client("secretsmanager", region_name=REGION)
    secret = json.loads(sm.get_secret_value(SecretId=SECRET)["SecretString"])
    secret.update(DT_MCP_URL=url, DT_MCP_TOKEN=token)
    sm.put_secret_value(SecretId=SECRET, SecretString=json.dumps(secret))
    print(f"stored DT_MCP_URL and DT_MCP_TOKEN in {SECRET}; test query returned {probe['row_count']} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
