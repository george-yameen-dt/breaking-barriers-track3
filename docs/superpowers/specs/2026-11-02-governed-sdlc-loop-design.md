# Governed SDLC loop for network agents: design

team04 · primary track: Track 3 · 2026-11-02 · status: draft for team review (open as a PR)

## 1. Why

Operators will not let AI agents change a live network unless every change is governed. The agents will
also keep changing: new prompts, new models, NLMs tuned by Track 1. Today a network change is "hope for the
right change and response". This design replaces hope with **predict, approve, act, verify**, and it makes
*improving the agents* cross the same checkpoint as *changing the network*.

> Dynatrace tells the agents what is true and later confirms whether the fix worked. Arize scores the agents
> and decides what they learn from. AWS enforces that every change, to a tower or to the swarm itself, is
> decided by policy, approved by the right person, and recorded.

The operator challenges from the event briefing, and where this design answers them:

| Challenge | Answer here |
|---|---|
| The right data in the operator's (and the agent's) hands | Dynatrace problem summary, log patterns, anomaly detection and cell/tower topology: deterministic and cheap in tokens |
| "Hope for the right change and response" | A twin preview before the change (predict), the Dynatrace outcome after it (verify) |
| Optimize for app traffic (video-heavy vs real-time voice) | The business intent sets the weights of the outcome score |
| Impact depends on the business case and technical viability | Intent → outcome score; twin → viability |
| Autonomous networks need control and governance | One checkpoint, three classes of change, three approvers |

The demo already contains the video-vs-voice tension: radio model v2 makes served phones about 3x faster and
drops about a third of them. For video that looks like a win; for voice it is an outage. Whether v2 is
"broken" depends on the intent, which is why the intent is configuration, not code.

## 2. Goals, non-goals, anti-goals

**Goals**

- **G1.** The Track 3 hand-in stays green, and gets its one missing proof: an incident run approved by a person typing `yes`.
- **G2.** One full, measured turn of the loop, with run ids: incident → outcome verified → scored → dataset →
  candidate → experiment → governed promotion → re-run on the frozen test set.
- **G3.** Two scores on every incident: the judge score (an LLM from another model family) and the outcome
  score (plain code over Dynatrace data).
- **G4.** Governed promotion of an agent change: the same checkpoint, an AI engineer's approval, an evidence
  threshold, an audit record.
- **G5.** The Track 1 hand-off: a training-ready export of the dataset, and a slot in the gate where a
  SageMaker model package is a candidate type.
- **G6.** Generalization: a use case is configuration (intent, KPIs, tools, policies); the loop does not change.

**Non-goals**

- Track 2 (skipped by the team).
- Training a Track 1 model. We build and exercise the hand-off; the training step is handed off.
- Bridge RL training, unless the hosted Maveric twin details arrive and someone is free (phase 2).
- Any claim about a real network. Every network here is a simulator.

**Anti-goals** (a failure even if it works)

- Breaking existing evidence. `track3/policies/track3.cedar` and `track3/scenarios/*.json` must not change:
  `tools/rebuild.py` checks their hashes against every declared run.
- An improvement loop that can approve itself, widen any agent's permissions, or edit policies.
- Claiming an improvement without a declared target, a frozen test set and an independent grader.
- An LLM evaluator from the gpt-oss family grading the gpt-oss agents.
- People's names in code, logs or records. Team code only; approvers are recorded by role and login.

## 3. Constraints

- Hand-in **12:00 PT, 3 Nov**. Account read-only **18:00 PT, 3 Nov**; endpoints deleted 18:05.
- The judges' live test is read-only: S3, CloudWatch Logs Insights, DynamoDB. The official record stays in AWS;
  Dynatrace and Arize are mirrors of it.
- Region us-east-2. Service roles under `/bb26/` with `bb26-Boundary`. No IAM users, no access keys.
- The whole team signs in with one role (`bb26-Participant`). The approver's identity comes from the approval
  record (role + login), not from IAM. This is declared as a gap.
- AgentCore Runtime `track3_agents` packages `checkpoint.py` and `incident.py`. Changing them in the cloud
  needs `agentcore/build.sh` and a new runtime version. Local (`--local`) runs need no deploy.
- Dynatrace reads use dtctl's current context (`DTCTL_CONTEXT` pins another); it must point at tenant yqs73977.
- Synthetic data only.

## 4. Architecture

The loop has eight stages: **Sense → Decide → Predict → Approve → Act → Verify → Learn → Promote.**

| Component | Role in the loop | State today |
|---|---|---|
| Simulated network (mobile-env on a laptop: 7 towers, 15 phones) | "Production" for the demo | Built |
| Digital twins (organisers' Maveric for training and preview, Viavi for operate) | Predict: try a fix before asking for approval | Not connected; Maveric details requested |
| Dynatrace (OpenPipeline, Smartscape, Davis, Workflows) | Sense: problem, cell metrics, topology. Verify: outcome queries. Receives every checkpoint record. Target: a Workflow starts the swarm | Sense built; Verify new; Workflow is a spike |
| Swarm on AgentCore Runtime `track3_agents` (network agent, change agent) | Decide | Built |
| Tupl swarm (triage → scalability / performance / throughput) | Advisors to the change agent, behind the checkpoint | Deployed, not behind the checkpoint |
| Checkpoint (`checkpoint.py`) | Cedar policy sets, Bedrock Guardrails, registry, approvals, audit to CloudWatch, DynamoDB, S3, Dynatrace and Arize | Built; promotion policy set new |
| AgentCore Gateway `track3-gw` with Policy (Cedar, ENFORCE) | Second wall on tools | Built |
| Arize (project `bb26-hackathon`) | Traces, judge and outcome scores on the trace, Signal issues (withdraw), datasets, experiments, prompt optimization | Traces and Signal built; rest new |
| Improvement loop (`improve.py`) | Learn: dataset, experiment, export. Promote through the checkpoint | New |
| SageMaker Model Registry | Where Track 1 NLMs and RL policies arrive as candidates (`PendingManualApproval`) | Slot only (phase 3) |

## 5. Three classes of change: same door, different key

| Change | Example | Approver | Evidence the approver sees | Enforced by |
|---|---|---|---|---|
| **Network** | Roll back the radio model | Network operator | Dynatrace now; twin preview of the fix | Checkpoint Cedar (`track3.cedar`) and AgentCore Gateway Cedar |
| **Agent** | New prompt, new model, new runtime version | AI engineer | Experiment result ≥ declared target on the frozen test set; no open critical/high Arize Signal issue | Checkpoint Cedar (`promotion.cedar`); registry; later the AgentCore endpoint moves to the new version; SageMaker approval for trained models |
| **Policy** | Edit a Cedar rule | Risk owner | Code review | Git and the AgentCore Policy engine; the loop has no path to it |

The rule that makes self-improvement safe: **self-improvement can change how the swarm thinks, never what it is
allowed to do.** A promoted agent still needs the operator's `yes` for every network change.

Separation of duties, written as Cedar in `promotion.cedar`:

- only an approval with role `ai_engineer` can promote;
- an agent can never promote anything (no self-approval);
- a network operator's approval cannot promote an agent change;
- nothing in the loop can act on a `Policy` resource;
- promotion needs `eval_passed` and zero open blocking Signal issues.

## 6. The two scores

**Judge score.** `us.anthropic.claude-haiku-5-5` grades the diagnosis and the fix against the measured facts,
1 to 5. It is a model's opinion: useful, but it can be wrong.

**Outcome score.** Plain code over Dynatrace data after the executed fix. No model involved, so it is an
independent grader by construction. For intent `coverage_first` (real-time voice):

- `restored`: `phones_dropped` is 0 for at least 3 consecutive minutes, starting within 6 minutes of the fix;
- `problem_closed`: the Davis problem is closed within 10 minutes;
- `no_regression`: the median served rate after the fix stays above a floor;
- score = weighted sum; verdict `pass` when `restored` and `no_regression` hold.

Intent `throughput_first` (video) weights the median rate instead. Intents live in `track3/intents/*.json`.

**When the two disagree,** the run is labelled for the dataset:

| Judge | Outcome | Label | What it teaches |
|---|---|---|---|
| pass | pass | `confirmed_good` | A training example (Track 1 export) |
| pass | fail | `sounded_right_failed` | The most valuable hard case |
| fail | pass | `judge_miscalibrated` | Fix the judge |
| fail | fail | `confirmed_bad` | A negative example |

A verify run attaches the outcome to the incident's trace id, so the outcome score appears on the same trace
as the judge score in Arize and Dynatrace.

## 7. The improvement turn (phase 1 target)

1. **Incident** runs as today. A new **verify** run reads the outcome from Dynatrace and records it under the
   incident's trace id.
2. **Dataset**: one item per incident and compare candidate, with inputs, agent outputs, both scores and the
   label → `evidence/datasets/<id>.jsonl`, hashed, and uploaded to Arize.
3. **Candidate**: version 2 of the network agent's diagnose prompt, from Arize prompt optimization or written
   by the AI engineer from the dataset's failures. The change agent's prompt stays as it is: it deliberately
   asks for the forbidden fix first, so every run exercises the deny. Improving it would remove a Track 3 test.
4. **Experiment**: the current prompt vs the candidate on `scenarios/frozen-state.json`, N repeats each.
   Scored by the judge (Claude), by affected-phones F1 against the measured `dropped_phones` (code), and by
   tokens and latency. The target is declared before the run.
5. **Promote**: through the checkpoint with `promotion.cedar`. An AI engineer types `yes`; the registry
   flips; the record goes to Dynatrace as a change event.
6. **Re-run** the incident with v2 and compare it with v1.
7. **Export**: a Track 1 fine-tuning file (chat JSONL) from the `confirmed_good` items, with a manifest: sha256,
   source run ids, purpose and licence. Listed in `DATA.md`.

Where prompts live: the text is in git (`track3/prompts/<agent>/<version>.txt`). The registry holds one item
per version (`prompt/<agent>@<version>`: status, sha256, text) and one pointer (`prompt/<agent>`: active
version). Agents read the active version at run start and record its version and hash.

## 8. Evidence

- New run kinds: `verify`, `experiment`, `promote`, `dataset`. Each is declared before it starts, like today.
- New rule-based units in `tools/check.py`: `outcome`, `experiment`, `promote` (and its separation-of-duties deny).
- `tools/rebuild.py` checks the policy file named in each declaration (default `policies/track3.cedar`), so old
  runs still rebuild and new runs can declare `policies/promotion.cedar` too.
- `judging.json` lists the new run ids. `GAPS.md` lists what did not work.

## 9. Phases and workstreams

**Phase 0: must have by 10:00 PT, 3 Nov.** A human-approved incident run (a person types `yes`). The demo
video recorded with the commit and run id on screen. `judging.json` updated. No new code needed.

**Phase 1: the loop, today.** The workstreams are independent; one person each.

| Workstream | Builds | Done when |
|---|---|---|
| WS-A Outcome | `outcome.py`, `verify` command, outcome on the trace in Arize | A verify run passes on a real incident run |
| WS-B Promotion gate | `promotion.cedar`, checkpoint policy sets, registry for prompts, `promote` command | A live promote allowed with AI-engineer approval, and a live deny for an operator approval and for a failing candidate |
| WS-C Arize learn | `dataset`, `experiment`, `export` commands, Arize dataset and experiment | v1 vs v2 measured on the frozen state, with a declared target |
| WS-D Dynatrace control plane | A Workflow that starts `track3_agents` when the problem opens (a spike first: can Dynatrace call AWS here without access keys?) | A run started by Dynatrace, or a recorded gap |
| WS-E Tupl behind the checkpoint | The Tupl specialists called as advisors through the checkpoint | One incident where the change agent consulted a specialist, recorded |

**Phase 2: if time allows.** A twin preview (what-if on the laptop sim; Maveric once its `.env` arrives).
Agents read the active prompt on AgentCore (redeploy as runtime version 2, then move the endpoint). Promotions
as Dynatrace change events. An approver role in the network policy (as a new policy file, never by editing
`track3.cedar`). The organisers' TeleLogs NLM on a SageMaker endpoint as a second candidate.

**Phase 3: roadmap slide.** A Track 1 fine-tune from the export. The RL policy as a governed tool. A second
intent (video vs voice) on the same loop.

## 10. Calls made by the builder (check these)

- Promotion policies go in a new file, `policies/promotion.cedar`, not in `track3.cedar`, to protect old evidence.
- The phase 1 candidate is the network agent's diagnose prompt, not a model, and not the change agent's prompt.
- Prompts live in the existing registry table (`bb26-team04-track3-registry`); no new table.
- A verify run is a separate run that reuses the incident's trace id. It does not append to the incident run,
  whose trace check expects `run_end` last.
- The outcome thresholds (3 minutes clean, within 6 minutes, closed within 10) come from the simulator's timing
  in `practice/README.md` (problems close about 2 minutes after the last violation, plus Grail delay).
- Approver role comes from a command-line flag plus a person typing `yes`. With one shared IAM role this is
  recorded, not enforced by IAM; declared in GAPS.md.

## 11. Open questions for the team

- Who owns which workstream?
- The hosted Maveric twin `.env` (twin URL, bearer token, team code). Is there a separate Bridge kit for the 0.73 target?
- Is the TM Forum autonomous-network language worth using in the pitch?

## 12. Decision (2026-11-02): an autonomous network, a human-approved governance layer

A judge told the team they are looking for an **autonomous network**, and that human approval belongs to **changes
to the governance layer** (for the evidence chain). This replaces the guide's "a person approves any change of
state" for network changes. The guide line is quoted, and this decision cited, in README and GAPS.md.

**The rule.** Agents change the network on their own, but only inside an envelope that Cedar enforces. People
change the envelope. Every autonomous network change records the hash of the policy that allowed it, and that
policy's activation record names the risk owner who approved it, so every change traces back to a person's `yes`.

| Change | Who decides | Enforced by |
|---|---|---|
| Network change inside the envelope | The orchestrator, autonomously; then verify, auto-revert if verification fails | Active swarm policy (Cedar), risk checks in code |
| Network change outside the envelope | Denied. The orchestrator may file a governance change request; it can never activate one | Active swarm policy; `governance.cedar` |
| Cedar policy set (swarm) | Risk owner, after validation, invariants and an impact preview, bound to the candidate's sha256 | `policies/governance.cedar` |
| Guardrail version | Risk owner, after the adversarial set is still blocked and the normal set still passes | `policies/governance.cedar` |
| Agent change (prompt, model) | AI engineer (unchanged, §5) | `policies/promotion.cedar` |
| `governance.cedar` itself | Code review in Git only; the flow cannot amend its own rules | `governance.cedar` forbids it |

**The envelope** (`policies/swarm-autonomous.cedar`, schema `swarm-autonomous.cedarschema`): the orchestrator only; the code risk
checks passed; power change of at most 3 dB, tilt change of at most 2°, or switching a cell back on; at most one
autonomous change per run; the existing forbids stay (power 30–46 dBm, tilt 0–10°, protected cell A, 15-minute
stabilization). Revert stays: only after verification failed, only to the exact old value.

**Policy versions.** Every activated policy is stored by hash (`policies/versions/<sha256>.cedar`, S3
`governance/policies/<sha256>.cedar`); the registry table holds the active pointer. Runtimes load the active
version at run start and refuse one whose hash does not match. Rebuild resolves each run's policy by the hash in its
declaration, so old evidence still rebuilds after a policy changes.

**Dynatrace reads are audited in the records.** Each dtctl call is a Cedar decision and a `tool_call` record (DQL,
its sha256, result sha256) under the run's trace id. That is enough; no pairing with Dynatrace's own query log.

**The decision tree is the record.** Every branch is its own Cedar action (`policies/swarm-autonomous.cedar`):
propose → select_proposal → apply_cell_configuration → verify → revert or hold, or request_governance_change when the
fix is outside the envelope. Each context carries `proposal_id`, so one run's decision records rebuild the tree,
with the `@id` of the rule that decided each node. The demo shows the network change happen and then walks that tree.

**Not in scope** (declared in GAPS.md): the AgentCore Gateway Cedar for the swarm tools; one IAM role per agent;
the approver's role enforced by IAM (still a flag plus a typed `yes`).
