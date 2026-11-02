# Handoff: the five-agent swarm (Track 3)

**Where:** branch `swarm-3to1`, PR #3, https://github.com/george-yameen-dt/breaking-barriers-track3/pull/3. `main` is untouched. The swarm doesn't edit `checkpoint.py` or `track3.py`, so it doesn't clash with Aeric's PR #2.

**Deadline:** the AWS account goes read-only at **12:00 Nov 3** (judging deck), so record the demo video before then.

## What it is (one minute)
We break a simulated cell network on purpose (tower C off). Five AI agents find it and propose a fix. A person approves that exact fix. Cedar rules decide what may be applied. Every step is checked and logged under one trace id.

| Agent | Job | Model | AgentCore runtime |
|---|---|---|---|
| Orchestrator (boss) | sends one snapshot out, picks one fix, asks a person | gpt-oss-120b | `track3_orchestrator` |
| Coverage | tower power and tilt | gpt-oss-120b | `track3_coverage` |
| Capacity | crowded towers (the pause demo withdraws its model) | gpt-oss-20b | `track3_capacity` |
| Availability | dead towers | gpt-oss-120b | `track3_availability` |
| Risk critic | reviews every proposal (the checks themselves are code) | gpt-oss-120b | `track3_risk` |

**Flow:**
1. Snapshot (hashed).
2. The 3 specialists work in parallel; none sees the others' answers.
3. Code checks every proposal (`risk_checks.py`).
4. The Risk critic assesses each one.
5. The orchestrator picks one fix (code guards the pick).
6. A deny probe (B at 50 dBm, rule `power-range`).
7. A person approves the proposal, bound to its sha256 hash.
8. Cedar allows the change, and the run reports "ready to apply".
9. A stabilization probe is denied.
10. A judge (Claude Haiku) and a rule grader grade the run.

## Run it
You need:
- an AWS SSO login to **bb26-team04** (role bb26-Participant), region us-east-2;
- Python 3.12 with `pip install -r track3/agentcore/requirements.txt`.

`swarm.sh` uses George's paths (`~/fyuz.env` and `~/.local/venvs/mobile-env`). On your machine:
```bash
cd track3
export AWS_PROFILE=<your bb26-team04 profile> AWS_REGION=us-east-2
python swarm.py run --cloud --declare --fault outage-C            # judged run on AgentCore; you type yes
python swarm.py run --cloud --declare --fault outage-C --pause    # pause demo
python swarm.py run --cloud --fault outage-C --approve-as test-unattended   # quick dev run, not declared
python tests/test_swarm.py                                        # 36 tests, no AWS
python tools/rebuild.py                                           # reprints results from evidence/, no AWS
```
Every judged run prints PASS/FAIL for `swarm_deny`, `swarm_allow`, `swarm_stabilization`, `swarm_trace` and `judge`.

The last automated test run, `2026-11-02T12-33-track3-swarm-01`, passed all five.

**Changing the agents:**
1. Edit `agents/` (the prompts are in `agents/prompts/*.md`).
2. Run `agentcore/build_swarm.sh` (zip to S3).
3. Run `python aws/swarm_setup.py` (redeploys the 5 runtimes).

## Where things are in AWS (us-east-2)
- **The agents:** https://us-east-2.console.aws.amazon.com/bedrock-agentcore/agents?region=us-east-2. Filter on `track3_`.
- **The audit trail:**
  - CloudWatch log group `/bb26/team04/track3`, one stream per run id.
  - DynamoDB `bb26-team04-track3-records`. The item with seq `0000` is the run's record counter.
- **Approvals:** DynamoDB `bb26-team04-track3-approvals` (key: run_id + approval_id, with `proposal_sha256`).
- **Evidence:** S3 `bb26-p-team04-data-279760855394-use2`, under `evidence/declared/` and `evidence/runs/`.
- **AWS changes made on Nov 2:**
  - the 5 runtimes;
  - two permissions added to role `team04-track3-runtime` (invoke the `track3_*` runtimes, update the records counter);
  - CloudWatch Transaction Search turned on, so the AgentCore pages show runs.

## Not done yet (in priority order)
1. **Your own judged runs** (type `yes`), then put the run ids in `judging.json` and copy it to S3 `judging/`.
2. **Hands:** apply the approved change through the AgentCore Gateway, then verify and auto-undo. Runs stop at "ready to apply" today, so the network map doesn't change.
3. **Eyes:** read the KPIs from Dynatrace with dtctl.
   - The tools exist in `dt_tools.py` and are switched off.
   - They need a Dynatrace read token in secret `team04/track3`.
   - Today's input is a recorded snapshot (`adapters/replay.py` + `rfmodel.py`).
4. **Traces in Dynatrace:** needs a Dynatrace OTLP token.
5. **Dynatrace app:** the "Checkpoint" tab still reads the Nov 1 record kinds.
6. **Gaps:**
   - one IAM role per agent (all 5 share one today);
   - `power-E-high` sometimes picks the wrong fix;
   - tilt and hotspot faults aren't modelled;
   - `--pause` hasn't been tried on AgentCore yet (it passes locally).

**Also running:** the Tupl Agent Builder EC2 (`i-04fae3625744457a6`, about $19/day) is separate and not used by the swarm. Terminate it, don't stop it, when you're done.
