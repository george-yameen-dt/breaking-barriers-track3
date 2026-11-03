# Autonomous network fix on the Viavi twin: detect, correlate, test, apply, audit

Date: 2026-11-03. Status: draft for approval. Deadline: repo and bucket are final at 12:00 Nov 3 (Seattle).

## Intent

Prove that agents can be trusted to change a network. A Viavi AI RAN fault shows up as anomalous KPIs in Dynatrace. Dynatrace
raises one problem with Smartscape context, a workflow wakes the agent swarm, the swarm picks a fix, tests the risk on the
digital twin, applies it through the Viavi MCP, checks the result and undoes it if it got worse. No person approves. Cedar
decides what the agents may do, and every step is audited and traced end to end in Dynatrace and Arize.

## Goals

- Detection by Davis anomaly detection on extracted metrics, per cell. No alert comes from a log field.
- One problem per incident, even when the fault hits several cells (S3 dark, S4 and S1 overloaded).
- Smartscape down to the cell: `CELL_NETWORK` > `CELL_TOWER` (site) > `CELL` (e.g. `S1/B3/C1`), with neighbour links between cells.
- The agents choose the fix. The expected one is more `tx_power` (or less tilt) on a neighbour, e.g. S4 40 to 44 dBm.
- Cedar enforces read-only analysis (dtctl and Dynatrace MCP) and the envelope for the one write tool.
- Every trace is exported to Dynatrace and Arize, however the swarm was started.
- A story that scales: nothing is hard-coded to four cells.

## Non-goals

- Production-grade Viavi hosting, a second RSG instance, or more than the 4-cell scenario in the demo.
- Extra governance beyond what decision 8 needs. The earlier plan's change control (policy versions by hash, risk-owner approval) stays only for AgentCore changes; guardrail promotion and the decision-tree view are optional.
- Restoring a dead tower. The twin cannot switch a cell on remotely; neighbours cover the gap.

## Anti-goals

- A "reset to healthy" button presented as the fix. The point is that agents make a reasoned change.
- A human approval step for network changes, in the code, the prompts or the README. (Governance-layer changes do need one: decision 8.)
- Alerts that exist only as a log field, or traces that exist only for "declared" runs.

## Decisions (the user's)

1. The poller on the team EC2 (`adapters/twin_dt.py`, `scenarios/twin/run8.py`) may change, but additively; OpenPipeline changes are preferred.
2. Viavi MCP is at `http://10.0.1.59/mcp` (nginx on `sswitalski-EC2`, port 80, open only to `team04-track3-runtime-sg`). Auth: none seen.
3. The tenant is `fyuz` (dtctl context `fyuz`, `yqs73977`). AWS profile `fyuz`, `us-east-2`.
4. Event correlation uses `builtin:event-correlation-rules` (early adopter on this tenant).
5. All agents get read-only dtctl and Dynatrace MCP. Cedar enforces it if it can.
6. No human in the loop. The dead pieces are removed.
7. All traces are always exported. `OTEL_SERVICE_NAME` is `multi-agent-swarm`.

8. Human approval applies to the governance layer only: any change to AgentCore (runtimes, Cedar policy sets, Gateway policy,
   guardrail version, the model/tool registry). Network changes are autonomous inside the Cedar envelope. This was agreed with
   a cohort lead; `GAPS.md` records the conversation (date, the lead's role, what was agreed) so the departure from "a person
   approves any change of state" is declared and not found. The existing `promotion.py` flow (hash-bound approval of changes to the agents, `promotion.cedar`) is the one human-approval
   demo; the earlier plan's `change_control.py` for Cedar policy sets is a stretch goal, not required.
9. Critical cells are implemented (see below).

## Critical cells

Viavi has no notion of a hospital or a 911 cell, so criticality is metadata we own, labelled synthetic in the README.
- `track3/scenarios/twin/critical-cells.json`: cell (`S2/B3/C1` style full name) to `{class: emergency|hospital|military|none, name}`.
  Demo default (my call): one `hospital`, one `emergency`, two `none`; the fault and the proposed fix touch both a normal and a
  critical neighbour so the policy visibly matters.
- Dynatrace: the poller sends `twin.criticality`; the pipeline stores it on the `CELL` node. Detection and correlation keep
  it as an event field, so a problem touching a critical cell is marked and ranked higher.
- Cedar: criticality is a resource attribute of the cell. Forbid any power decrease or tilt increase on a critical cell. Forbid a
  change to a neighbour when the twin prediction lowers a critical cell's QoS or raises its phones without service. Critical
  cells get the tighter step limits (power at most +4 dB, my call). A change outside this stays denied; it is never approved by a
  person, because the human gate is for governance only.
- Agents: the risk analyst reads criticality from the Smartscape attribute and states it in its assessment; the orchestrator
  must rank fixes that protect critical cells first.
- The audit record carries `criticality` for every cell a decision touches.

## Judging requirements this design must meet

- Every model, tool and agent-to-agent call crosses the checkpoint (Cedar, Bedrock Guardrails, audit record with one trace id
  in CloudWatch Logs). That includes dtctl, Dynatrace MCP and Viavi MCP calls.
- Registered before callable: each new tool (each allowed dtctl command, Dynatrace MCP tool, Viavi MCP tool) is registered and
  approved in the registry; anything unregistered is denied.
- A live deny and a live allow with decision records; the `--pause` demo (withdraw a model, calls refused); `tools/rebuild.py`
  follows one trace id from the Dynatrace problem through the workflow and Lambda to the Viavi command.
- Four units, each with all four parts (declared target in `evidence/declared/<run_id>.json` before the run, output by repo
  code, per-item records keyed to one run id that agree with AWS times, a pass/fail check). Candidate units: detection within N
  minutes; one problem for one incident; fix verified (phones without service down, critical cells not worse); the deny probe
  and trace rebuild. Thresholds are declared before the run. Records carry wall-clock times, since Viavi's are simulated.
- The read-only live test uses only S3, CloudWatch Logs Insights and DynamoDB. Dynatrace-derived facts the agents used
  (problem id, DQL results, Viavi command responses) are copied into the audit records; Dynatrace and Arize are demo views.
- The independent judge stays a different model family from the agents.
- Submission: `README.md` (first line the primary track, "Before Nov 1" note), `judging.json` (up to 20 run ids; copy in S3 under
  `judging/`), `DATA.md`, `GAPS.md`, a demo recording with commit and run id on screen, everything pushed before 12:00 Nov 3 Seattle.
- `GAPS.md` declares: the cohort-lead conversation on human approval; the twin test is a model prediction; the Viavi MCP has
  no auth; causality rules if unavailable; simulated timestamps; critical-cell labels are synthetic.

## Design

**Poller (small, additive).** Read the real name from the Viavi cell report (`Viavi.Cell.Name`, e.g. `S4/B3/C1`) and send
new fields beside the old ones: `twin.cell_full`, `twin.site`, `twin.band`, `twin.pci`, `twin.nrcgi`, `twin.frequency`.
Discover the cell list from the data and drop the fixed `CELLS` tuple and the `{id}/B3/C1` guess in `adapters/twin.py`. Old
fields stay, so the pipeline and dashboards keep working.

**OpenPipeline (`gy-viavi-twin`, `scenarios/twin/dynatrace/pipeline.json`).**
- Add a `CELL` node per `twin.cell_full`, parented to its `CELL_TOWER` (site); move neighbour edges to cell level.
- Add the cell as a dimension on the 8 `gy_twin_m_*` metrics (check what they carry now) and add the metrics the detectors need.
- Remove the two log-field Davis events (`gy_twin_tower_down`, `gy_twin_phones`).

**Detection.** Davis anomaly detectors (`builtin:davis.anomaly-detectors`) on the extracted metrics per cell: QoS score
down, phones without service up, RSRP p10 down, a cell going dark (power or load at zero). Replace `detector.json`, the
record-based log detector. Each event carries the cell's Smartscape id so the problem lands on the right entity.

**Event correlation.** Rule 1: cell anomaly events grouped by `dt.smartscape.cell_network`, 5-minute window, so one fault is one
problem. At scale the grouping moves to site or neighbour cluster (my call). If causality rules work on this tenant (the
schema flags them `IN_DEVELOPMENT`), add "cell down causes neighbour overload"; if not, plain grouping is enough.

**Trigger.** Keep `workflows/track3-problem-trigger.yaml` and `aws/trigger_lambda.py`. The payload must carry the problem id,
the root-cause entity and the affected cells so the swarm starts from the correlated problem, not from one cell.

**Agents (rework of `agents/*`).**
- Orchestrator plus coverage, capacity, availability specialists and a risk analyst. All can call Dynatrace MCP and dtctl,
  read-only: DQL, problems, Smartscape, metrics. No other Dynatrace tool.
- Specialists propose changes inside their domain. The orchestrator picks one.
- Risk is tested on the twin before the change is applied (see open decision below).
- The change is applied with the Viavi MCP `simulation_command` (`tx_power`, `antenna_tilt`). A later window is measured; if
  QoS or phones-without-service got worse, the change is undone (the `rollback` kept with each proposal).
- The approval step (`ask_person`, `approval_valid`, `approval_requested`) is removed. `swarm-autonomous.cedar` becomes
  `swarm.cedar` (the plan's Task 2 already built it and its 43 tests).

**Cedar.**
- Dynatrace: allow only the named read-only dtctl commands and MCP tools; deny every other tool by default.
- Viavi: allow `scenario_list`, `simulation_list`, `simulation_status`, `simulation_query`, `get_available_actions`,
  `reference`, `get_schema`, `list_kpis`, `list_use_cases`, and `simulation_command` only for `tx_power` and `antenna_tilt`
  inside the envelope (power 30-46 dBm, at most +8/-6 dB per change; tilt 4-10 degrees, at most 5 per change; 15-minute
  stabilization; one autonomous change per run). Deny `simulation_start`, `simulation_stop`, `scenario_save`, `scenario_update`,
  `scenario_clone`, `scenario_delete`, `simulation_advance`. Reaching the Viavi MCP goes through an AgentCore Gateway target
  inside the VPC, so the Gateway policy is a second wall.

**Tracing.** Remove the per-run filter (`telemetry._RunFilter`) and the "declared run" condition: every run exports to
CloudWatch, Dynatrace and Arize. Remove the dev-run token stripping in `swarm.py` (line ~70) and the missing-key bypass. Set
`OTEL_SERVICE_NAME=multi-agent-swarm`: in `telemetry.py` for the laptop provider (now `track3-checkpoint`) and as an
environment variable on each AgentCore runtime (ADOT reads it; set in `aws/swarm_setup.py`). One service name means the agent
is told apart by span attributes (`gen_ai.agent.name`), so each agent span must carry its name (my call). If an exporter has no
credentials, the run fails loudly and records `trace_export_unavailable` in the audit log; it never skips silently (my call).

**Cleanup.** Remove `practice/` (mobile-env `sim.py`, `agent.py`, `control.json` path, old OpenPipeline files), `adapters/replay.py`,
`rfmodel.py` and the frozen scenarios if nothing else uses them, `aws/tools_lambda.py` (DynamoDB `rollback_model`), the old
README claims about approval, and tests for removed code. Keep what the twin path uses. Remove in an order that keeps the
tests running (my call).

## Decided: how the fix is tested on the twin

Viavi allows one run at a time, and `run8.py` refuses to start while a run is active. The live fault is itself a Viavi run,
so a throwaway second run would stop it (`force=true` did exactly that in my test). The user chose option A: predict with the
existing what-if (`TwinNetwork.simulate` and `neighbor_impact`) on the live windows, apply, then measure the next window and
undo if worse. Honest wording: a twin-model prediction plus a measured check. No extra run. (Option B, a second RSG instance,
is out of scope.)

## `simulation_command` and what Cedar allows

The Viavi tool takes `command_type` (handover, turn_on, turn_off, antenna_tilt, tx_power, energy_saving, cell_sleep_mode,
antenna_mask, slice_prb_quotas, slice_qos_profile, handover_policy, conditional_handover, cho_override, min_time_of_stay, raw),
`params`, an optional `delay` and a `reason` that is logged with the command. It confirms a command was accepted, not that the
RAN applied it, so the measured next window is the real check. The server's own ranges (tx_power 0-50 dBm, tilt -15..15) are
wider than the envelope (30-46 dBm, 4-10 degrees), and UE names are not validated, so Cedar adds real limits.
- Allow: `tx_power` and `antenna_tilt`, inside the envelope.
- Deny: `raw` (it would bypass every other rule), `turn_off`, `turn_on`, `handover*`, `cho_override`, `energy_saving`,
  `cell_sleep_mode`, `antenna_mask`, slice commands. Widening later is a one-line policy change.
- Require `reason` to carry the run's trace id, and `delay` to be absent or short, so the Viavi event log matches the audit
  record (my call).

## Left to the builder

Exact Davis detector thresholds and models; the matching-condition syntax for correlation rules; Cedar action naming; how the
Dynatrace MCP and dtctl reach AgentCore (Gateway target or in-runtime tool); the order of cleanup commits.

## Risks

- The tenant's correlation causality rules may not be enabled.
- The Viavi MCP accepts calls without auth; Cedar and the security group are the only controls.
- Timestamps from Viavi are simulated (2024-01-01); the poller already maps them (check before relying on metrics).
- About ten hours remain. Cut order if late: cleanup of non-blocking files, causality rules, the second twin option.
