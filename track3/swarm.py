"""Runs the Track 3 swarm on this laptop: five agents, every model and tool call through the checkpoint.

  ./swarm.sh run --fault outage-C                   you approve at the keyboard (type yes)
  ./swarm.sh run --fault outage-C --approve-as X    unattended test, recorded as such
  ./swarm.sh run --fault outage-C --pause           Capacity answers once, its model is withdrawn, the run goes on without it
  ./swarm.sh run --fault power-E-high --dynatrace   the specialists may also query Dynatrace with dtctl

The five agents (agents/README.md): orchestrator, coverage, capacity, availability and the risk critic. For now they
run in this one process (the plan's fallback: one runtime, same contracts); one AgentCore runtime per agent comes next.
These are dev runs: not declared, records in dev-runs/<run_id>/records.jsonl and in the audit log (CloudWatch
/bb26/team04/track3, DynamoDB), with no Dynatrace or Arize mirror.
"""

import argparse
import getpass
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config

import checkpoint
from adapters.replay import ReplayNetwork
from agents.governance import POLICIES, SwarmCheckpoint
from agents.orchestrator import run_incident
from checkpoint import now_iso

HERE = Path(__file__).parent
DEV_RUNS = HERE / "dev-runs"
PROMPTS = HERE / "agents" / "prompts"


def new_run_id() -> str:
    stamp, n = datetime.now().strftime("%Y-%m-%dT%H-%M"), 1
    while (DEV_RUNS / f"{stamp}-track3-swarm-dev-{n:02d}").exists():
        n += 1
    return f"{stamp}-track3-swarm-dev-{n:02d}"


def file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def approver(approve_as: str):
    def ask(req: dict):
        if approve_as:
            return approve_as, True, "unattended test with --approve-as: no person at the keyboard", "cli", req["proposal_sha256"]
        p = req["proposal"]
        print(f"\n  The orchestrator asks you to approve {req['approval_id']}:")
        print(f"    change     {p['cell_id']} {p['parameter']} {p['current_value']} -> {p['proposed_value']}  (from {p['agent']})")
        print(f"    why        {req['rationale']}")
        print(f"    predicted  phones without service {req['predicted']} · risk {req['risk']}")
        print(f"    hash       {req['proposal_sha256']}")
        answer = input("  Type yes to approve, anything else to decline: ").strip().lower()
        return getpass.getuser(), answer == "yes", "typed at the keyboard", "cli", req["proposal_sha256"]
    return ask


def cmd_run(a) -> None:
    for k in ("DT_PLATFORM_TOKEN", "DT_TRACES_TOKEN", "ARIZE_API_KEY"):  # dev runs: no operator-view mirrors
        os.environ.pop(k, None)
    checkpoint.ARIZE_KEY_FILE = HERE / ".no-arize-key-for-dev-runs"
    run_id, trace_id = new_run_id(), uuid.uuid4().hex
    cp = SwarmCheckpoint(run_id, trace_id, DEV_RUNS / run_id)
    net = ReplayNetwork(a.fault, run_id, now_iso())
    print(f"Run {run_id} (dev run, not declared) · trace {trace_id}")
    cp.record("run_start", mode="swarm", fault=a.fault, fault_sha256=file_sha(net.fault_path),
              layout_sha256=file_sha(HERE / "scenarios" / "swarm-layout.json"), policies_sha256=file_sha(POLICIES),
              prompts_sha256={p.stem: file_sha(p) for p in sorted(PROMPTS.glob("*.md"))}, operator=getpass.getuser(),
              dynatrace_tools=a.dynatrace, where="laptop: five agents in one process (dev run, not declared)")
    try:
        result = run_incident(cp, net, approver(a.approve_as), use_dynatrace=a.dynatrace,
                              pause_operator=getpass.getuser() if a.pause else "")
    except Exception as e:  # a failed run is a result too: close it properly
        cp.record("run_error", error=str(e)[:500])
        result = {"outcome": f"error: {e}"}
        print(f"Run failed: {e}")
    cp.record("run_end", outcome=result.get("outcome"))
    print(f"Outcome: {result.get('outcome')}")
    print(f"Records: {cp.local.relative_to(HERE)} ({cp.seq}) · CloudWatch {checkpoint.LOG_GROUP}, stream {run_id}")


REGION = "us-east-2"
APPROVALS_TABLE = "bb26-team04-track3-approvals"


def short(r: dict) -> str | None:
    """One line per record that matters to a person watching the run; None for the routine ones."""
    k = r["kind"]
    if k == "handoff":
        return f"hand-off {r['from_agent']} -> {r['to_agent']}"
    if k == "specialist_report":
        rep = r["report"]
        return f"{r['who']} reports: {rep.get('domain_verdict')} · {len(rep.get('proposals') or [])} proposal(s)"
    if k == "risk_assessment":
        return f"{r['proposal_id']}: risk {r['risk']} · {r['recommendation']}"
    if k == "recommendation":
        return f"orchestrator recommends {r.get('proposal_id')}: {r.get('rationale', '')}"
    if k == "deny_probe":
        return f"deny probe {r['cell_id']} {r['parameter']} -> {r['new_value']}: {r['decision']} ({', '.join(r['policies'])})"
    if k == "approval_decision":
        return f"approval {r['status']} by {r['approved_by']} · hash {r['proposal_sha256'][7:19]}…"
    if k == "apply_ready":
        return f"Cedar allows the change: {r['cell_id']} {r['parameter']} {r['old_value']} -> {r['new_value']} (ready to apply)"
    if k == "stabilization_probe":
        return f"same cell again right after: {r['decision']} ({', '.join(r['policies'])})"
    if k == "judged":
        return f"judge: {r.get('score')}/5 {r.get('verdict')}: {r.get('reason')}"
    if k in ("degraded", "refused", "run_error", "agent_failed"):
        return f"{k}: {r.get('missing') or r.get('who', '')} {r.get('reason') or r.get('error', '')}"[:200]
    if k in ("run_start", "run_end"):
        return f"{k} ({r.get('where') or r.get('outcome')})"
    return None


def follow(run_id: str, done: threading.Event, approve_as: str) -> list:
    """Prints the runtimes' records as they land, and answers the orchestrator's approval request."""
    ddb = boto3.client("dynamodb", region_name=REGION)
    last, recs = "0000", []
    while True:
        finished = done.is_set()
        items = ddb.query(TableName=checkpoint.RECORDS_TABLE, KeyConditionExpression="run_id = :r AND seq > :s",
                          ConsistentRead=True, ExpressionAttributeValues={":r": {"S": run_id}, ":s": {"S": last}}).get("Items", [])
        for it in items:
            rec, last = json.loads(it["record"]["S"]), it["seq"]["S"]
            recs.append(rec)
            line = short(rec)
            if line:
                print(f"  {rec['seq']:>4} {line}")
            if rec["kind"] == "approval_requested":
                p = rec["proposal"]
                if approve_as:
                    who, ok, note = approve_as, True, "unattended test with --approve-as: no person at the keyboard"
                else:
                    print(f"\n  The orchestrator (on AgentCore) asks you to approve {rec['approval_id']}:")
                    print(f"    change  {p['cell_id']} {p['parameter']} {p['current_value']} -> {p['proposed_value']}  (from {p['agent']})")
                    print(f"    why     {rec.get('orchestrator_rationale', '')}")
                    print(f"    hash    {rec['proposal_sha256']}")
                    ok = input("  Type yes to approve, anything else to decline: ").strip().lower() == "yes"
                    who, note = getpass.getuser(), "typed at the keyboard"
                ddb.put_item(TableName=APPROVALS_TABLE, Item={
                    "run_id": {"S": run_id}, "tool": {"S": rec["approval_id"]}, "approved": {"BOOL": ok}, "approver": {"S": who},
                    "note": {"S": note}, "channel": {"S": "cli"}, "proposal_sha256": {"S": rec["proposal_sha256"]},
                    "at": {"S": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}})
        if finished and not items:
            return recs
        time.sleep(1)


def cmd_run_cloud(a) -> None:
    """The same run on AgentCore: the orchestrator's runtime calls the other four runtimes."""
    arns = json.loads((HERE / "aws" / "swarm-runtimes.json").read_text())
    run_id, trace_id = new_run_id(), uuid.uuid4().hex
    out_dir = DEV_RUNS / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"mode": "swarm", "run_id": run_id, "trace_id": trace_id, "fault": a.fault, "taken_at": now_iso(),
               "pause": a.pause, "operator": getpass.getuser()}
    print(f"Run {run_id} on AgentCore (one runtime per agent, dev run) · trace {trace_id}")
    box, done = {}, threading.Event()

    def go():
        try:
            client = boto3.client("bedrock-agentcore", region_name=REGION,
                                  config=Config(read_timeout=900, connect_timeout=15, retries={"total_max_attempts": 1}))
            r = client.invoke_agent_runtime(agentRuntimeArn=arns["orchestrator"], qualifier="DEFAULT",
                                            runtimeSessionId=f"{run_id}-{trace_id[:12]}", contentType="application/json",
                                            accept="application/json", payload=json.dumps(payload).encode())
            box["result"] = json.loads(r["response"].read().decode() or "{}")
        except Exception as e:  # shown after the records, so the person sees how far the run got
            box["error"] = str(e)[:500]
        finally:
            done.set()

    threading.Thread(target=go, daemon=True).start()
    recs = follow(run_id, done, a.approve_as)
    (out_dir / "records.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in recs))
    print(f"Outcome: {box.get('result', {}).get('outcome') or box.get('result') or box.get('error')}")
    print(f"Records: {(out_dir / 'records.jsonl').relative_to(HERE)} ({len(recs)}) · CloudWatch {checkpoint.LOG_GROUP}, stream {run_id}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Track 3 swarm: five agents, governed by the checkpoint")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="one incident: snapshot, three specialists, risk critic, recommendation, approval")
    r.add_argument("--fault", default="outage-C", help="a fault in scenarios/faults (outage-C, power-E-high)")
    r.add_argument("--approve-as", default="", help="approve without a person (unattended test, recorded as such)")
    r.add_argument("--pause", action="store_true", help="withdraw Capacity's model after it answers once")
    r.add_argument("--dynatrace", action="store_true", help="give the specialists dtctl tools and the Dynatrace for AI skills")
    r.add_argument("--cloud", action="store_true", help="run on AgentCore: one runtime per agent (aws/swarm-runtimes.json)")
    a = ap.parse_args()
    cmd_run_cloud(a) if a.cloud else cmd_run(a)


if __name__ == "__main__":
    main()
