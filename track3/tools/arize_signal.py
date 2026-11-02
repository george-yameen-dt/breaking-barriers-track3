"""Open Arize Signal issues on the hackathon project, read through Arize's GraphQL API.

Signal is Arize's automated trace reviewer. It reads the project's traces on a schedule and files issues
(severity, status, example trace ids). `blocking()` returns the open critical and high ones, which
`./track3.sh signal` turns into a registry withdrawal that the checkpoint then enforces.
"""

import json
import os
import urllib.request

from checkpoint import ARIZE_KEY_FILE, ARIZE_PROJECT, ARIZE_SPACE_ID

GRAPHQL = "https://app.arize.com/graphql"
BLOCKING = {"critical", "high"}
QUERY = """query($spaceId: ID!) { node(id: $spaceId) { ... on Space {
  agentIssues(limit: 500) { id title severity status projectName traceIds firstSeen lastSeen totalMatchCount } } } }"""


def issues() -> list:
    key = os.environ.get("ARIZE_API_KEY") or ARIZE_KEY_FILE.read_text().strip()
    req = urllib.request.Request(GRAPHQL, data=json.dumps({"query": QUERY, "variables": {"spaceId": ARIZE_SPACE_ID}}).encode(),
                                 headers={"x-api-key": key, "Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    if r.get("errors"):
        raise RuntimeError(f"Arize GraphQL: {r['errors'][0].get('message', r['errors'])}")
    return [i for i in r["data"]["node"]["agentIssues"] if i["projectName"] == ARIZE_PROJECT]


def blocking() -> list:
    return [i for i in issues() if i["status"] == "OPEN" and i["severity"] in BLOCKING]
