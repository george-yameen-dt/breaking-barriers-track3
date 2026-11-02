# What did not work (so far)

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
- **Person's approval:** every approval so far is an unattended test (`approver: test-unattended` or `claude-test-unattended`). A run approved by a person typing `yes` is still to do. With the agents on AgentCore, the approval travels from the laptop through DynamoDB `bb26-team04-track3-approvals`.
- **Run start:** the operator's laptop reads Dynatrace and starts the run. A Dynatrace Workflow that starts it when the problem opens is designed, not built.
- **Dynatrace traces:** we have no Dynatrace token with `openpipeline:traces:ingest` scope. Dynatrace shows the chain as log records keyed by `trace_id`. Spans go to Arize only.
- **Approver role (promotion):** the whole team signs in with one role (`bb26-Participant`), so the approver's role (`ai_engineer`, `network_operator`) is declared on the command line and recorded with the login on each approval. The promotion policy enforces it; IAM does not.
- **Promoted prompts on AgentCore:** promotion flips the registry, but the runtime `track3_agents` (version 1) still runs the diagnose prompt built into its code. Agents reading the active prompt at run start needs a rebuild and runtime version 2 (planned).
- **Outcome score, experiment, Track 1 export:** `outcome.py` scores a recovery from Dynatrace data and is tested offline; the `verify`, `experiment` and `export` commands are not built yet. No Track 1 model is trained: the export is the hand-off.

