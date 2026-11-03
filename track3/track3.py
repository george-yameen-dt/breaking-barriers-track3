"""Breaking Barriers Track 3: agents that act on a network only through a checkpoint.

The five-agent swarm runs with ./swarm.sh (swarm.py). This file keeps the model registry, the Arize Signal gate and
the promotion flow. The first two-agent incident, its runtime and its policy file (track3.cedar) were removed.

  ./track3.sh setup            register the two models as approved (audited); also how a withdrawn model is restored
  ./track3.sh signal [--watch 60]   an open critical/high Arize Signal issue on bb26-hackathon withdraws the agent model
  ./track3.sh register-prompt --agent network-agent --version v2    a prompt version becomes a candidate (audited)
  ./track3.sh promote --agent network-agent --version v2 --role ai_engineer --experiment-run <id>
                               an agent change goes live only through policies/promotion.cedar and a person's yes
  python3 tools/show_record.py --run-id <id>    read-only record viewer (the judges' live test)
  python3 tools/rebuild.py                      reprint every result from evidence/, no AWS

Every run is declared in evidence/declared/<run_id>.json (and in S3) before it starts.
"""

import argparse
import getpass
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3

from checkpoint import ARIZE_PROJECT, PROMOTION_POLICIES, REGION, Checkpoint, Refused
from incident import AGENT_MODEL, DIAGNOSE, JUDGE_MODEL
from promotion import PromptRegistry, blocking_issue_count, experiment_passed, promote
from tools import arize_signal, check

HERE = Path(__file__).parent

SCENARIO = HERE / "scenarios" / "incident-v2-cutoff.json"
BUCKET = os.environ.get("BUCKET", "bb26-p-team04-data-279760855394-use2")
DECLARED_S3 = ""  # S3 LastModified of the current run's declaration, written into its run_start record


def code_commit() -> str:
    """The git commit the run used (shown on screen with the run id for the demo video)."""
    import subprocess
    r = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    # New evidence files (declarations, records) are written by every run; only code changes make a run "dirty".
    dirty = subprocess.run(["git", "-C", str(HERE), "status", "--porcelain", "--untracked-files=no", "--", ".", ":(exclude)evidence"],
                           capture_output=True, text=True).stdout.strip()
    return (r.stdout.strip() + ("-dirty" if dirty else "")) if r.returncode == 0 else "not-in-git"


def file_sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def new_run_id(kind: str) -> str:
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M")
    n = 1
    while (HERE / "evidence" / "declared" / f"{stamp}-track3-{kind}-{n:02d}.json").exists():
        n += 1
    return f"{stamp}-track3-{kind}-{n:02d}"


def declare(run_id: str, units: list, targets: dict, extra_hashes: dict | None = None, models: list | None = None) -> Path:
    """Written before the run starts: what we expect, against which frozen scenario (and, for a promotion, which policies)."""
    decl = {
        "run_id": run_id, "track": "track-3", "units": units, "target": targets,
        "test_set_sha256": file_sha(SCENARIO), "test_set": str(SCENARIO.relative_to(HERE)),
        "model_id": AGENT_MODEL, **(extra_hashes or {}),
        **({"candidate_models": models} if models else {}),
        "grader": {"rules": "tools/check.py on the decision records",
                   "llm_judge": f"{JUDGE_MODEL} (Anthropic family; the agents run on OpenAI gpt-oss)"},
        "declared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = HERE / "evidence" / "declared" / f"{run_id}.json"
    path.write_text(json.dumps(decl, indent=2) + "\n")
    s3 = boto3.client("s3", region_name=REGION)
    s3.upload_file(str(path), BUCKET, f"evidence/declared/{run_id}.json")
    # S3 stamps whole seconds. Start the run only once we are clearly past AWS's own time for the declaration,
    # so the AWS times show "declared before the run" without any rounding doubt.
    import time
    last_modified = s3.head_object(Bucket=BUCKET, Key=f"evidence/declared/{run_id}.json")["LastModified"]
    while datetime.now(timezone.utc).timestamp() < last_modified.timestamp() + 1.5:
        time.sleep(0.2)
    global DECLARED_S3
    DECLARED_S3 = last_modified.isoformat()
    return path


def upload_run(run_id: str, out_dir: Path) -> None:
    s3 = boto3.client("s3", region_name=REGION)
    for f in out_dir.iterdir():
        s3.upload_file(str(f), BUCKET, f"evidence/runs/{run_id}/{f.name}")


def cmd_setup(_args) -> None:
    run_id = new_run_id("setup")
    declare(run_id, ["registry"], {"registry": f"{AGENT_MODEL} and {JUDGE_MODEL} approved in the model registry"})
    cp = Checkpoint(run_id, uuid.uuid4().hex, HERE / "evidence" / "runs" / run_id)
    cp.record("run_start", declared_s3_last_modified=DECLARED_S3, purpose="register models")
    for m in (AGENT_MODEL, JUDGE_MODEL):
        cp.set_model_status(m, "approved", getpass.getuser())
    tools = {k: v for k, v in json.loads((HERE / "policies" / "tools.json").read_text()).items() if not k.startswith("_")}
    names = [t if group == "dtctl" else f"{group}:{t}" for group, ts in tools.items() for t in ts]
    for t in names:
        cp.register_tool(t, "approved", getpass.getuser())
    cp.record("run_end")
    upload_run(run_id, cp.out_dir)
    print(f"{run_id}: both models and {len(names)} tools approved")


# ---- agents on AgentCore -------------------------------------------------------------------------


def report(run_id: str, units: list) -> None:
    path = HERE / "evidence" / "runs" / run_id / "records.jsonl"
    if not path.exists():
        return
    recs = check.load(path)
    for u in units:
        ok, why = check.UNITS[u](recs)
        print(f"  {u:<12} {'PASS' if ok else 'FAIL'}  {why}")


def signal_once(dry_run: bool) -> None:
    """One check: an open critical or high Arize Signal issue withdraws the agent model; the checkpoint then refuses its calls."""
    found = arize_signal.blocking()
    stamp = datetime.now().strftime("%H:%M:%S")
    if not found:
        print(f"{stamp} Signal: no open critical or high issue on {ARIZE_PROJECT}")
        return
    for i in found:
        print(f"{stamp} Signal: {i['severity']} · {i['title']} ({len(i['traceIds'] or [])} traces)")
    if dry_run:
        print(f"{stamp} dry run: would withdraw {AGENT_MODEL}")
        return
    run_id = new_run_id("signal")
    declare(run_id, ["signal"], {"signal": f"an open critical or high Arize Signal issue on {ARIZE_PROJECT} withdraws {AGENT_MODEL} "
                                           "in the registry, so the checkpoint refuses its calls until a person approves it again"})
    cp = Checkpoint(run_id, uuid.uuid4().hex, HERE / "evidence" / "runs" / run_id)
    cp.record("run_start", declared_s3_last_modified=DECLARED_S3, purpose="Arize Signal gate", commit=code_commit())
    if cp.model_status(AGENT_MODEL) != "approved":
        print(f"{stamp} {AGENT_MODEL} is already {cp.model_status(AGENT_MODEL)}")
    else:
        for i in found:
            cp.record("signal_issue", issue_id=i["id"], title=i["title"], severity=i["severity"], project=ARIZE_PROJECT,
                      trace_ids=(i["traceIds"] or [])[:10], last_seen=i["lastSeen"])
        cp.set_model_status(AGENT_MODEL, "withdrawn", "arize-signal")
        print(f"{stamp} {AGENT_MODEL} withdrawn. A person restores it with ./track3.sh setup once the issue is handled.")
        try:  # proof in the same run: the agent's next call is refused by the checkpoint
            cp.call_model("network-agent", AGENT_MODEL, DIAGNOSE, '{"check": "is the network healthy?"}')
            print("UNEXPECTED: the call went through")
        except Refused as e:
            print(f"{stamp} next agent call refused: {e}")
    cp.record("run_end")
    upload_run(run_id, cp.out_dir)
    print(f"run {run_id}")


def cmd_signal(args) -> None:
    while True:
        signal_once(args.dry_run)
        if not args.watch:
            return
        time.sleep(args.watch)


PROMPTS = HERE / "prompts"


def cmd_register_prompt(args) -> None:
    path = PROMPTS / args.agent / f"{args.version}.txt"
    if not path.exists():
        sys.exit(f"{path.relative_to(HERE)} does not exist")
    run_id = new_run_id("registry")
    declare(run_id, ["registry"], {"registry": f"prompt {args.agent}@{args.version} registered as a candidate; nothing goes live"},
            extra_hashes={"candidate_sha256": file_sha(path)})
    cp = Checkpoint(run_id, uuid.uuid4().hex, HERE / "evidence" / "runs" / run_id)
    cp.record("run_start", declared_s3_last_modified=DECLARED_S3, purpose="register a prompt candidate", commit=code_commit())
    item = PromptRegistry(cp.ddb).register(args.agent, args.version, path.read_text(), getpass.getuser())
    cp.record("registered", agent=args.agent, version=args.version, sha256=item["sha256"], status=item["status"])
    cp.record("run_end")
    upload_run(run_id, cp.out_dir)
    print(f"run {run_id}: {args.agent}@{args.version} is a candidate (sha256 {item['sha256'][:12]})")


def approve_promotion(role: str, approve_as: str):
    def ask(agent: str, version: str, evidence: dict):
        if approve_as:
            return approve_as, role, True, "approved with --approve-as (unattended test, no person at the keyboard)"
        print(f"\n  The improvement loop wants to make {agent}@{version} live. Evidence: {json.dumps(evidence)}")
        answer = input(f"  You are approving as {role}. Type yes to approve, anything else to decline: ").strip().lower()
        return getpass.getuser(), role, answer == "yes", "typed at the keyboard"
    return ask


def cmd_promote(args) -> None:
    candidate = PROMPTS / args.agent / f"{args.version}.txt"
    exp_records = HERE / "evidence" / "runs" / args.experiment_run / "records.jsonl" if args.experiment_run else None
    eval_passed = experiment_passed(check.load(exp_records), args.agent, args.version) if exp_records and exp_records.exists() else False
    requested_by = f'Agent::"{args.agent}"' if args.as_agent else 'Loop::"improvement"'
    unit = "promote" if args.role == "ai_engineer" and not args.as_agent else "promote_denied"
    run_id = new_run_id("promote")
    declare(run_id, [unit], {unit: (f"{args.agent}@{args.version} goes live only after an AI engineer approves and the evidence holds "
                                    "(policy promote-needs-ai-engineer-and-evidence)") if unit == "promote" else
                             f"promoting {args.agent}@{args.version} requested by {requested_by} with role {args.role} is denied by a named "
                             "rule in policies/promotion.cedar, and nothing goes live"},
            extra_hashes={"promotion_policies_sha256": file_sha(PROMOTION_POLICIES),
                          **({"candidate_sha256": file_sha(candidate)} if candidate.exists() else {})})
    cp = Checkpoint(run_id, uuid.uuid4().hex, HERE / "evidence" / "runs" / run_id)
    cp.record("run_start", declared_s3_last_modified=DECLARED_S3, purpose=f"promote {args.agent}@{args.version}", commit=code_commit())
    evidence = {"eval_passed": eval_passed, "open_blocking_issues": blocking_issue_count(), "experiment_run_id": args.experiment_run}
    if evidence["open_blocking_issues"] < 0:
        print("  Arize Signal could not be read (set ARIZE_API_KEY; it is in the team04/track3 secret). "
              "The promotion policy treats that as a refusal.")
    if not eval_passed:
        print(f"  No passing experiment_result for {args.agent}@{args.version} in run '{args.experiment_run}'. "
              "The promotion policy treats that as a refusal.")
    rec = promote(cp, PromptRegistry(cp.ddb), args.agent, args.version, evidence, approve_promotion(args.role, args.approve_as), requested_by)
    cp.record("run_end", outcome=rec["kind"])
    upload_run(run_id, cp.out_dir)
    print(f"run {run_id}: {rec['kind']}" + (f" ({', '.join(rec['policies'])})" if rec.get("policies") else ""))
    report(run_id, [unit])


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    s = sub.add_parser("signal")
    s.add_argument("--watch", type=int, default=0, help="check again every N seconds")
    s.add_argument("--dry-run", action="store_true", help="report blocking issues, change nothing")
    rp = sub.add_parser("register-prompt")
    rp.add_argument("--agent", required=True)
    rp.add_argument("--version", required=True)
    pr = sub.add_parser("promote")
    pr.add_argument("--agent", required=True)
    pr.add_argument("--version", required=True)
    pr.add_argument("--role", required=True, choices=["ai_engineer", "network_operator"], help="the approver's role (recorded)")
    pr.add_argument("--experiment-run", default="", help="the experiment run whose result is the evidence")
    pr.add_argument("--approve-as", default="", help="unattended test only: approve in this name instead of asking")
    pr.add_argument("--as-agent", action="store_true", help="live deny: the agent asks to promote itself")
    a = ap.parse_args()
    {"setup": cmd_setup, "signal": cmd_signal, "register-prompt": cmd_register_prompt, "promote": cmd_promote}[a.cmd](a)


if __name__ == "__main__":
    main()
