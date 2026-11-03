# What did not work (so far)

## Autonomous Viavi run (2026-11-03): what limits it, declared up front

- **Human approval, a conversation with a cohort lead.** The written criteria list "a human approval before any change of state". We discussed this with a cohort lead on 2026-11-03 [name and role of the lead, and the time of the conversation: the team fills these in]. The guidance was that human approval means approval of changes to the **governance layer** (changes to the agents themselves: prompts, policies, the registry, the guardrail version, AgentCore), and that the **network** changes are to be autonomous. We follow that. Network changes inside the Cedar envelope are applied by the orchestrator with no person (unit `swarm_autonomous_apply` checks that no `approval_requested` record exists); promoting an agent change still needs a person's `yes` (`policies/promotion.cedar`, unit `promote`). We declare the departure so it is not found.
- **The "twin test" is a model prediction, not a second run.** Viavi allows one run at a time and the live fault is that run, so a throwaway second run would stop it (we showed this: starting another run with `force=true` stopped a faulty run at 151 s). The risk of a change is predicted by the twin what-if model (`TwinNetwork.simulate`, the changed cell's phones only; neighbours are not predicted) and then **measured one window after the change**; a change that makes the network or a critical cell worse is undone. A critical-cell harm check on neighbours is a proxy (the change adds interference while a critical neighbour's edge SINR is at or below -3 dB).
- **The Viavi MCP has no authentication.** It sits behind an nginx proxy on the team EC2 (`team04-viavi-proxy`, port 80) and is reachable only from the runtimes' security group. The controls are that security group and Cedar (read allow-list, `tx_power` and `antenna_tilt` only, trace id in the reason).
- **Agents on AgentCore do not read Dynatrace.** `dtctl` is read-only and works on laptop runs (`--dynatrace`); there is no dtctl binary in the AgentCore zip, and no Dynatrace MCP client was built. The agents get the problem from the Dynatrace workflow (its id, title, description with the cell names and criticality, and start time are in the alert text). Dynatrace and Arize are operator views of the run; the judges' live test reads S3, CloudWatch Logs Insights and DynamoDB, where every record the units use is written. We do not score what Dynatrace detected (no unit reads Dynatrace).
- **Event correlation uses grouping only.** The tenant's causality rules (`builtin:event-correlation-rules`, flagged in development) were not tried.
- **Simulated time.** Viavi's own timestamps are simulated (2024-01-01); the poller adds a wall-clock time (`twin.wall_time`) and every unit that compares times uses that.
- **Critical-cell labels are synthetic.** Viavi has no notion of a hospital or a dispatch centre; `scenarios/twin/critical-cells.json` is ours.
- **Scale limits.** The Dynatrace side supports several cells under one tower, but the twin adapter keys cells by site (one cell per site) and the pipeline reads three neighbour fields per cell. The 4-cell scenario fits both.
- **The attached twin does not know earlier agent changes** (`prior_commands` is empty), so the 15-minute stabilization rule cannot see a change made by an earlier run. The trigger Lambda starts one run per incident window (15 minutes) to cover this.
- **Pipeline ownership.** The teammate's `gy-viavi-twin` pipeline is not shared with our account (403), so we created `gy-viavi-twin-cells` and pointed the routing entry for `service.name viavi-twin` at it (`smartscape.sh undo` points it back). The teammate's earlier log-record detector may still exist; we cannot see or delete it.
- **Old resources remain.** The AgentCore Gateway `track3-gw` and the Lambda `team04-track3-tools` from the earlier design are still deployed; nothing calls them. Runs declared on 2026-11-01 and 2026-11-02 were approval-based, on the replay network; they stay as they are and still rebuild (their policy is archived by hash under `policies/versions/`).

## 2026-11-01T14-41-track3-incident-01: judge call failed
- **Unit limited:** judge.
- **What happened:** the network agent, the change agent, the live deny (`restart_all_towers`, policy `no-network-wide-restart`) and an approved `set_tower_power` all completed and were recorded (seq 1–22). The judge call then failed with `ValidationException: temperature is deprecated for this model` (`us.anthropic.claude-haiku-5-5`, Bedrock Converse).
- **Fix:** `temperature` is now sent only to gpt-oss.
- **Same run, second issue:** the change agent's "safe fix" was `set_tower_power`, which the simulator cannot apply, so the network was not repaired. We now offer the agent only `restart_all_towers` and `rollback_model`.
- **The run is incomplete** (no `run_end` record). We keep it as it is.

## 2026-11-01T14-42-track3-incident-01: run stopped at start
- **Unit limited:** none (no decisions were made).
- **What happened:** a type error in our wait-for-outage check (`'>' not supported between str and int`) stopped the run after `run_start` and the `read_network` decision.
- **Fixed in code.**

## 2026-11-01T14-42-track3-incident-02: judge returned no answer
- **Unit limited:** judge.
- **What happened:** deny, approval, allow and rollback all passed. The judge model spent all 400 of its output tokens on reasoning and returned empty text, so the score is missing.
- **Fix:** the judge now gets 2,500 tokens.

## 2026-11-01T14-54-track3-incident-01: declaration time vs run start
- **Unit limited:** evidence chain (timing). Nothing limits deny, allow or trace.
- **What happened:** the declaration's S3 LastModified is 22:54:47Z, which is about 0.5 s *after* the run_start record (22:54:46.538Z, laptop clock). The local file says declared_at 22:54:44Z. The laptop clock was checked against time.apple.com at +0.04 s, so this is S3's whole-second stamp, not drift.
- **Fix:** from this point every run waits until it is 1.5 s past the declaration's S3 LastModified before it starts, and writes that LastModified into its run_start record.

## Any run: a failed model call left no record
- **Unit limited:** evidence chain.
- **What happened:** in 2026-11-01T14-41-track3-incident-01, the judge call failed at Bedrock (request 72f5cac2-93d0-47ef-ae19-7731c295abb7, ValidationException). Only CloudTrail shows it; our records stop at the input guardrail.
- **Fix:** the checkpoint now writes a `model_call_failed` record with the request id before it raises.

## 2026-11-01 19:07 PT: role creation denied (platform rule, fixed)
- **Unit limited:** none (setup only).
- **What happened:** `iam:CreateRole` for `bb26/bb26-team04-track3-runtime` returned AccessDenied (explicit deny in the participant policy, statement `NeverTouchEventPlumbing`, which reserves role names starting with `bb26-`).
- **Fix:** the roles are named `team04-track3-runtime`, `team04-track3-tools` and `team04-track3-gateway`, still under `/bb26/` with the `bb26-Boundary`.

## 2026-11-01 19:10 PT: AgentCore Policy rejected the "never restart" rule (fixed)
- **Unit limited:** none.
- **What happened:** `CreatePolicy` for `no_network_wide_restart` ended `CREATE_FAILED`: the Cedar analyzer flags a forbid that denies every request for a tool as "Overly Restrictive". That is the intent.
- **Fix:** created with `--validation-mode IGNORE_ALL_FINDINGS`. Tested: the Gateway refuses `restart_all_towers` and allows `rollback_model`.

## Closed on 2026-11-01 19:20 PT: AgentCore
- The agents and the checkpoint now run on AgentCore Runtime `track3_agents` (role `team04-track3-runtime`, created by us under `/bb26/` with the boundary). Tools sit behind AgentCore Gateway `track3-gw`, with Policy in AgentCore (`track3_policies`, ENFORCE). First runs: `2026-11-01T19-18-track3-pause-01`, `2026-11-01T19-20-track3-incident-01`, all units pass.
- Runs before 19:18 PT ran the agents on the laptop; we keep them as they are.

## Open gaps
- **Enforcement of model calls:** tool calls are now enforced by AWS at the Gateway. Model calls are still enforced only by our checkpoint: the same participant role can call Bedrock directly. Ten manual `aws` CLI test calls on Nov 1 (13:46, 14:37, 15:45 PT) did exactly that, outside any run.
- **Person's approval:** closed for network changes on 2026-11-03 (see the first section); promotion approvals stay human.
- **Run start:** closed on 2026-11-03: a Dynatrace workflow starts the run when the correlated problem opens (`workflows/track3-problem-trigger.yaml`).
- **Dynatrace traces:** we have no Dynatrace token with `openpipeline:traces:ingest` scope. Dynatrace shows the chain as log records keyed by `trace_id`. Spans go to Arize only.
- **Approver role (promotion):** the whole team signs in with one role (`bb26-Participant`), so the approver's role (`ai_engineer`, `network_operator`) is declared on the command line and recorded with the login on each approval. The promotion policy enforces it; IAM does not.
- **Promoted prompts on AgentCore:** promotion flips the registry, but the runtime `track3_agents` (version 1) still runs the diagnose prompt built into its code. Agents reading the active prompt at run start needs a rebuild and runtime version 2 (planned). (That runtime was deleted on 2026-11-02; see the next section.)
- **Outcome score, experiment, Track 1 export:** `outcome.py` scores a recovery from Dynatrace data and is tested offline; the `verify`, `experiment` and `export` commands are not built yet. No Track 1 model is trained: the export is the hand-off.

## Removed on 2026-11-02: the first two-agent version
- Deleted: AgentCore Runtime `track3_agents`, `policies/track3.cedar`, `agentcore/build.sh`, `aws/runtime-id.txt`, the commands `run`, `pause`, `compare` and `adversarial` in `track3.py` (and `--local`), the incident and pause modes of `agentcore/main.py`, and the eleven declared runs from `2026-11-01T14-40` to `2026-11-02T05-05` with their records and results. The swarm replaces it.
- The dated entries above describe that version and stay as written. They name runs that are no longer in `evidence/`; git history holds them. Copies of those records are still in S3 `evidence/`, DynamoDB `bb26-team04-track3-records` and CloudWatch `/bb26/team04/track3`; we did not delete them.
- `judging.json` lists no runs now. Put the swarm runs you report in `run_ids` and `reported_results` before submitting.
