# What did not work, and what limits the result

## Human approval: autonomous network changes (L4 automation)
- The written criteria list "a human approval before any change of state". On 2026-11-03 we asked Nick Thompson of AT&T about it. His answer: humans are needed for **policy changes**, and the goal is an autonomous system that changes the network **without needing a human**. That is L4 automation, which we treat as our innovation.
- We follow that. Network changes inside the Cedar envelope are applied by the orchestrator with no person (`swarm_autonomous_apply` checks that no `approval_requested` record exists). A change to the policies or to the agents needs a named person: a risk owner approves one exact policy file by its SHA-256 (`policies/governance.cedar`, units `governance_denied`, `governance_change`, `policy_pin`), and an AI engineer approves an agent change (`policies/promotion.cedar`).
- We declare the departure from the written wording so it is not found.

## What the agents do not do
- **Power and tilt in the cell table are not read back from the simulator.** The agents' cell table, and the poller's `twin.tx_power_dbm` and `twin.tilt_deg` in Dynatrace, use the planned values (40 dBm, 6 degrees) changed by the commands the twin accepted; a dark cell still shows 40 dBm, and the twin does not report tilt at all. What is measured is the signal: RSRP, SINR, QoS, throughput and the phones, and the 15 to 11 phones without service after the fix is measured from the next window. A teammate's change that reports measured power was not taken into the final build (evidence was produced without it).

## How a change is tested
- **The "twin test" is a model prediction, not a second run.** Viavi allows one run at a time and the live fault is that run, so a throwaway second run would stop it (starting another with `force=true` stopped a faulty run at 151 s). The risk of a change is predicted by the twin what-if model (the changed cell's phones only; neighbours are not predicted) and then **measured one window after the change**; a change that makes the network or a critical cell worse is undone. The critical-neighbour check is a proxy (the change adds interference while a critical neighbour's edge SINR is at or below -3 dB).
- **A run can end with no change.** Proposals are checked by code before Cedar sees them. A run on 2026-11-03 (17:23 UTC) ended "no safe proposal; nothing applied" because the agents proposed +4 dB on an emergency cell and the limit is +3 dB; the agents are now told the limit. Model output varies, so a run can still end this way.
- **The attached twin does not know earlier agent changes** (`prior_commands` is empty), so the 15-minute stabilization rule cannot see a change made by an earlier run. The trigger Lambda starts one run per incident window (15 minutes) to cover this.

## Platform and data
- **The Viavi MCP has no authentication.** It sits behind an nginx proxy on the team EC2 (port 80) and is reachable only from the runtimes' security group. The controls are that security group and Cedar (read allow-list, `tx_power` and `antenna_tilt` only, trace id in the reason).
- **Model calls are enforced by our checkpoint, not by AWS.** The runtimes' role can also call Bedrock directly; ten manual `aws` CLI test calls on Nov 1 did exactly that, outside any run. The AgentCore Gateway `track3-gw` and the Lambda `team04-track3-tools` from the first design are still deployed; nothing calls them.
- **Approver identity.** The whole team signs in with one role, so a policy approver is recorded by the laptop username on the command and the role declared on the command line. The policies enforce the role; IAM does not.
- **Event correlation** uses grouping by network plus one causality rule (a dark cell causes the other events). The problem can still absorb a later fault if the previous problem has not closed; between runs we ran a healthy scenario so problems close.
- **Simulated time.** Viavi's own timestamps are simulated (2024-01-01); the poller adds a wall-clock time (`twin.wall_time`) and every unit that compares times uses that.
- **Critical-cell labels are synthetic.** Viavi has no notion of a hospital or a dispatch centre; `scenarios/twin/critical-cells.json` is ours.
- **Scale.** The Dynatrace side supports several cells under one tower, but the twin adapter keys cells by site (one cell per site). The 4-cell scenario fits both.
- **Pipeline ownership.** A teammate's `gy-viavi-twin` pipeline is not shared with our account (403), so we created `gy-viavi-twin-cells` and pointed the routing entry for `service.name viavi-twin` at it (`smartscape.sh undo` points it back). That teammate's earlier log-record detector may still exist; we cannot see or delete it.
- **No Track 1 model is trained.** The agents use Bedrock models.

## Other runs in the bucket
- `evidence/` and DynamoDB hold more declared runs than `judging.json` reports: development runs from 2026-11-01 to 2026-11-03, including runs of the first approval-based version that was removed on 2026-11-02, and three runs on 2026-11-03 that were superseded (one crashed on start for a missing file in the build, one ran before the Dynatrace MCP was configured, one ended with no safe proposal). They are not reported. The reported runs are the three in `judging.json`.
