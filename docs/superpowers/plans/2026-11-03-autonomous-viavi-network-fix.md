# Autonomous Viavi Network Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Viavi fault becomes a correlated Dynatrace problem on a cell, which wakes the swarm; the swarm picks a fix, predicts its risk on the twin, applies it through the Viavi MCP inside a Cedar envelope (critical cells protected), verifies it, undoes it if worse, and every step is audited and traced.

**Architecture:** Most of the loop exists on `main`: `adapters/twin*.py` (poller, twin, `apply`/`measure`), `agents/orchestrator.py` (`apply_and_verify` with undo), `dt_tools.py` (read-only dtctl), `workflows/` + `aws/trigger_lambda.py` (problem trigger), and `policies/swarm-autonomous.cedar`. This plan swaps approval for the autonomous envelope, adds criticality, moves detection from log alerts to metric anomaly detection with correlation, makes tracing unconditional, then cleans up and writes the judging evidence.

**Tech Stack:** Python 3.12, cedarpy, boto3, Strands on AgentCore, OTel, dtctl, Dynatrace OpenPipeline and settings, Viavi MCP.

**Spec:** `docs/superpowers/specs/2026-11-03-autonomous-viavi-network-fix-design.md`

## Global Constraints

- Deadline: repo and bucket final at **12:00 Nov 3 (Seattle)**; account read-only from 18:00 Nov 3. Push before then; a demo from a later build loses marks.
- AWS profile `fyuz`, region `us-east-2`. dtctl context `fyuz` (tenant `yqs73977`). Bucket `bb26-p-team04-data-279760855394-use2`.
- Viavi MCP: `http://10.0.1.59/mcp`; reachable only from `team04-track3-runtime-sg`. Stored scenarios: `team04-track3-4cell-healthy`, `-powercut`, `-faults`.
- Envelope: tx_power 30-46 dBm, change within -6..+8 dB; tilt 4-10 degrees, change at most 5; 15-minute stabilization; one autonomous change per run. Critical cells: no power decrease, no tilt increase, power increase at most +4 dB.
- `simulation_command` allow-list: `tx_power`, `antenna_tilt` only. Deny `raw`, `turn_off`, `turn_on`, `handover*`, `cho_override`, `energy_saving`, `cell_sleep_mode`, `antenna_mask`, slice commands, `simulation_start|stop|advance`, `scenario_save|update|clone|delete`. `reason` must contain the run's trace id.
- Human approval only for governance-layer changes (AgentCore, Cedar policy sets, guardrail version, registry). Network changes never ask a person.
- `OTEL_SERVICE_NAME=multi-agent-swarm`. Every run exports to CloudWatch, Dynatrace and Arize.
- Never edit `policies/promotion.cedar`. Tests: `cd track3 && uv run --python 3.12 --with-requirements tests/requirements.txt pytest -q tests`. `python3 tools/rebuild.py` exits 0 at the end of every task that touches records.
- Cut order if late: Dynatrace MCP (dtctl-only, declared in GAPS.md), causality rules, Gateway second wall, non-blocking cleanup.

## Review Focus

- A fix that helps a normal cell but lowers a critical cell's QoS must be denied (T2 `test_critical_harm_denies_neighbour_fix`).
- A cell name outside the 4-cell set (scale) must not crash the poller, registry or pipeline; criticality defaults to `none` (T2, T4).
- Missing Dynatrace or Arize credentials must fail the run loudly with a `trace_export_unavailable` record, never skip (T8).
- The workflow firing twice for the same problem must start one run (T7 `test_duplicate_problem_starts_one_run`).
- Viavi refusing a command, or no active run when applying, is recorded and the run ends cleanly (T1 `test_refused_apply_is_recorded`).

---

### Task 1: Autonomous apply replaces approval

**Files:** Modify `track3/agents/orchestrator.py` (drop `ask_person`, `APPROVAL_TTL`, approval ids; edit `apply_context`, `apply_and_verify`, `run_incident`), `track3/swarm.py` (drop `approver`, `--approve-as`), `track3/agents/cloud.py` (drop `wait_for_person`, approvals table), `track3/tools/check.py`; rename `policies/swarm-autonomous.cedar` to `swarm.cedar` and `swarm-autonomous.cedarschema` to `swarm.cedarschema` (replace the old files); Test `track3/tests/test_swarm.py`, `test_autonomous_policy.py`.

**Interfaces:**
- Produces: `apply_context(net, cell_id: str, parameter: str, value: int, risk_checks_passed: bool, applies_this_run: int, minutes=None, criticality: str = "none", critical_harm_predicted: bool = False) -> dict` (all keys the schema requires; `approval_valid` is gone); `apply_and_verify(run, chosen: dict, ctx: dict, predicted: str, say) -> str`; `check.swarm_autonomous_apply(recs) -> tuple`, `check.swarm_verify(recs) -> tuple`, both registered in `UNITS`.
- Envelope policy values are the Global Constraints, replacing the 3 dB / 2 degree values in the file.

- [ ] **Step 1: Failing tests:** `test_s4_power_40_to_44_allowed` (Allow by `apply-autonomous-within-envelope`), `test_power_above_46_denied` (`power-range`), `test_second_apply_same_cell_in_15m_denied` (`stabilization-15m`), `test_run_has_no_approval_records` (no `approval_requested`, `approval_granted` kinds in a full offline run), `test_refused_apply_is_recorded` (net reply `{"accepted": False}` gives an `applied` record and outcome text, no exception), `test_check_autonomous_apply_pass_and_fail`, `test_check_verify_revert_path`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** `apply_autonomously` is not a new function: `run_incident` calls `cp.decide(... "apply_cell_configuration" ...)` right after `recommend`/`deny_probe`, then `apply_and_verify`. Keep the stabilization probe and the deny probe. Old-run checks (`swarm_allow`) stay for rebuilding old declared runs.
- [ ] **Step 4: Run all tests and `python3 tools/rebuild.py`; expect PASS and exit 0.**
- [ ] **Step 5: Commit** `feat(track3): the swarm applies autonomously inside the Cedar envelope`.

### Task 2: Critical cells

**Files:** Create `track3/scenarios/twin/critical-cells.json`, `track3/adapters/criticality.py`; Modify `track3/adapters/twin.py` (`TwinNetwork.cell` adds `criticality`), `track3/risk_checks.py`, `policies/swarm.cedar` and `.cedarschema`, `agents/prompts/risk.md`, `agents/prompts/orchestrator.md`; Test `track3/tests/test_criticality.py`.

**Interfaces:**
- Produces: `adapters.criticality.load(path: Path = DEFAULT) -> dict[str, dict]` (`{"S2/B3/C1": {"class": "hospital", "name": "..."}}`); `criticality_of(cell_id: str) -> str` (accepts `S2` or `S2/B3/C1`; unknown gives `"none"`); `risk_checks.critical_harm(net, cell_id: str, parameter: str, value: int) -> bool` (true when `net.neighbor_impact` predicts a critical cell's QoS down or its phones without service up).
- Cedar policy ids: `critical-no-power-cut`, `critical-no-tilt-up`, `critical-power-step-4db`, `critical-neighbour-no-harm`.
- Demo default: S2 hospital, S4 emergency, S1 and S3 none.

- [ ] **Step 1: Failing tests:** `test_unknown_cell_is_none`, `test_short_and_full_names_match`, `test_critical_power_cut_denied` (id `critical-no-power-cut`), `test_critical_tilt_up_denied`, `test_critical_power_plus_5_denied_plus_4_allowed`, `test_critical_harm_denies_neighbour_fix` (S3 fix predicted to lower S4 QoS gives `critical-neighbour-no-harm`), `test_risk_check_lists_criticality`.
- [ ] **Step 2: Run; expect FAIL.** **Step 3: Implement** the files above; the context keys `criticality` and `critical_harm_predicted` come from Task 1's `apply_context`. **Step 4: Run tests; expect PASS.** **Step 5: Commit** `feat(track3): critical cells`.

### Task 3: Read-only tool policy and tool registry

**Files:** Modify `track3/policies/swarm.cedar` and `.cedarschema`, `track3/checkpoint.py`, `track3/dt_tools.py`, `track3/adapters/twin_link.py`; Create `track3/policies/tools.json`, `track3/tests/test_tool_policy.py`.

**Interfaces:**
- Consumes: Task 2's schema.
- Produces: Cedar actions `dt_read` (resource `Tool::"dtctl"` or `Tool::"dt-mcp:<tool>"`), `viavi_read` (`Tool::"viavi:<tool>"`), `viavi_command` (context `command_type: String`, `delay_s: Long`, `reason_has_trace_id: Bool`); `Checkpoint.tool_status(tool: str) -> str` (`"approved"` or `"unregistered"`, from the registry table, mirroring `model_status`); `dt_tools.dtctl_read(args: list[str], cp) -> dict` (allowed verbs `get`, `query`, `describe`; anything else raises `Refused` after a deny record). Policy ids: `tools-approved-only`, `dt-read-only`, `viavi-read`, `viavi-command-allowlist`, `viavi-trace-id-in-reason`.
- `policies/tools.json` lists each approved tool; `./track3.sh setup` writes it into the registry.

- [ ] **Step 1: Failing tests:** `test_unregistered_tool_denied`, `test_dtctl_delete_denied`, `test_dtctl_query_allowed`, `test_viavi_raw_denied`, `test_viavi_turn_off_denied`, `test_viavi_tx_power_allowed`, `test_viavi_command_without_trace_id_denied`, `test_viavi_simulation_start_denied`.
- [ ] **Step 2: Run; expect FAIL.** **Step 3: Implement.** Every Viavi call in `twin_link.py` goes through `cp.decide` first. Dynatrace MCP read tools (`execute-dql`, `query-problems`, `get-problem-by-id`, `find-documents`) are registered the same way if the endpoint and token are available; if not by 08:00 Seattle, ship dtctl-only and note it for `GAPS.md`. **Step 4: Run tests; PASS.** **Step 5: Commit** `feat(track3): read-only and allow-listed tools under Cedar`.

### Task 4: Poller sends real cell names, discovers cells

**Files:** Modify `track3/adapters/twin.py` (remove `CELLS`, `short`/`full` guessing), `track3/adapters/twin_dt.py` (`snapshot_records`), `track3/adapters/twin_link.py` (`HELPERS.window` returns `Viavi.Cell.Name`, PCI, NrCgi, frequency); Test `track3/tests/test_twin.py`.

**Interfaces:**
- Produces: cell records carry the old fields plus `twin.cell_full` (`S1/B3/C1`), `twin.site`, `twin.band`, `twin.pci`, `twin.nrcgi`, `twin.frequency`, `twin.criticality`, `twin.wall_time` (ISO, wall clock); `twin.snapshot(window, config, ...)` takes the cell list from `window`. Old field names stay.

- [ ] **Step 1: Failing tests:** `test_cells_discovered_from_window` (8-cell fixture gives 8 cells), `test_cell_record_has_full_name_and_site`, `test_old_fields_unchanged`, `test_record_has_wall_time`, `test_unknown_cell_name_does_not_crash`.
- [ ] **Step 2-4:** run FAIL, implement, run PASS. **Step 5: Commit** `feat(track3): poller sends Viavi cell names and discovers cells`. Redeploy to the EC2 with `scenarios/twin/deploy-ec2.sh` in Task 12.

### Task 5: OpenPipeline: Cell level, criticality, cell metrics, no log alerts

**Files:** Modify `track3/scenarios/twin/dynatrace/pipeline.json`, `smartscape.sh`; Test `track3/tests/test_pipeline_json.py`.

**Interfaces:** Produces processors `gy_twin_cell_node` (node type `CUSTOM_CELL`, id from `twin.cell_full`, parent the `CUSTOM_CELL_TOWER` for `twin.site`, fields `twin.criticality`, band, pci), neighbour edges at cell level, the six per-cell `gy_twin_m_*` metrics carrying the cell, and a new `gy_twin_m_dark` (1 when `twin.state == "DARK"`). Removes `gy_twin_tower_down` and `gy_twin_phones`.

- [ ] **Step 1: Failing tests:** `test_cell_node_processor_present`, `test_metrics_carry_cell_dimension`, `test_log_davis_processors_removed`, `test_every_processor_id_unique`.
- [ ] **Step 2-3:** FAIL, edit JSON. **Step 4:** `./smartscape.sh apply` then `dtctl query 'smartscapeNodes "CUSTOM_CELL"'` returns 4 cells after a run (Task 12 confirms live). **Step 5: Commit** `feat(track3): Smartscape cells and cell metrics in OpenPipeline`.

### Task 6: Metric anomaly detectors and event correlation

**Files:** Create `track3/scenarios/twin/dynatrace/detectors/{qos,phones,rsrp,dark}.json`, `correlation-rules.json`, `apply-detection.sh`; delete `detector.json`; Test `track3/tests/test_detection_json.py`.

**Interfaces:** Detectors use `builtin:davis.anomaly-detectors` on `gy_twin_m_qos`, `gy_twin_m_phones`, `gy_twin_m_rsrp`, `gy_twin_m_dark`, per cell, event carrying the cell's Smartscape id. Correlation rule (`builtin:event-correlation-rules`): displayName `Viavi cell anomalies per network`, matching condition on `dt.alert_group == "viavi-twin"`, groupingFields `["dt.smartscape.cell_network"]`, timeWindow `5m`. Causality rule "dark cell causes neighbour overload" only if the tenant accepts it; else omit and note for `GAPS.md`.

- [ ] **Step 1: Failing tests:** every detector JSON names its metric and a threshold or baseline; `test_correlation_rule_groups_by_network`; `test_no_detector_queries_logs`.
- [ ] **Step 2-3:** FAIL, write files; thresholds are chosen before the declared run (from the healthy and faulty runs already in `dev-runs/`). **Step 4:** apply with `dtctl apply -f` / `dtctl settings`; confirm with `dtctl get settings --schema builtin:event-correlation-rules`. **Step 5: Commit** `feat(track3): metric anomaly detection and event correlation`.

### Task 7: Workflow and trigger carry the correlated problem and trace id

**Files:** Modify `track3/workflows/track3-problem-trigger.yaml`, `track3/aws/trigger_lambda.py` (`alert_text`, `start_run`, `handler`), `track3/agents/cloud.py` (`handle`); Test `track3/tests/test_trigger.py`.

**Interfaces:** Payload adds `problem_id`, `root_cause_entity`, `affected_cells: list[str]`, `trace_id` (32 hex, derived as `sha256(problem_id)[:32]` so a repeat gives the same id). `trigger_lambda.start_run` returns the existing run for a `problem_id` already started (DynamoDB conditional put on the records table).

- [ ] **Step 1: Failing tests:** `test_duplicate_problem_starts_one_run`, `test_trace_id_is_stable_for_a_problem`, `test_alert_text_names_cells_and_criticality`.
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(track3): workflow starts one run per correlated problem`.

### Task 8: Tracing always on, service name set

**Files:** Modify `track3/telemetry.py` (remove `_RunFilter` gating), `track3/checkpoint.py` (`mirror_run`), `track3/swarm.py` (remove the token stripping in `cmd_run`), `track3/aws/swarm_setup.py` (runtime env `OTEL_SERVICE_NAME=multi-agent-swarm`), `track3/agents/governance.py` (span attribute `gen_ai.agent.name` on every agent span); Test `track3/tests/test_telemetry.py`.

**Interfaces:** `telemetry.provider()` builds its resource with `service.name` from `OTEL_SERVICE_NAME`, default `multi-agent-swarm`. `checkpoint.mirror_run(trace_id)` always mirrors both destinations and records `trace_export_unavailable` (and raises `Refused`) when a credential is missing.

- [ ] **Step 1: Failing tests:** `test_dev_run_exports_to_all_destinations`, `test_service_name_is_multi_agent_swarm`, `test_missing_arize_key_fails_loudly`, `test_agent_span_carries_agent_name`.
- [ ] **Step 2-4:** FAIL, implement, PASS. **Step 5: Commit** `feat(track3): every run is traced, service multi-agent-swarm`.

### Task 9: Viavi link from the VPC runtimes

**Files:** Modify `track3/adapters/twin_link.py` (add `HttpLink(rsg_url: str = "http://10.0.1.59/mcp")` with the same `run(op, timeout_s, program)` contract as `LocalLink`), `track3/agents/cloud.py`, `track3/aws/swarm_setup.py` (env `VIAVI_RSG_URL`), `track3/agentcore/build_swarm.sh`; Test `track3/tests/test_twin.py`.

- [ ] **Step 1: Failing tests:** `test_http_link_matches_local_link_ops` (a fake MCP HTTP server answers the ops `LocalLink` supports), `test_http_link_refuses_when_no_active_run`.
- [ ] **Step 2-4:** FAIL, implement (reuse `HELPERS`), PASS. Gateway second wall is optional (cut order). **Step 5: Commit** `feat(track3): swarm reaches the Viavi MCP from the VPC`.

### Task 10: Declared run, evidence and checks

**Files:** Modify `track3/swarm.py` (`declare`), `track3/tools/check.py`, `track3/tools/rebuild.py`, `track3/judging.json`; Test `track3/tests/test_rebuild.py`.

**Interfaces:** `evidence/declared/<run_id>.json` is written before the run with targets: problem raised within N minutes of the fault, exactly 1 problem, phones without service after fix at most the declared value, no critical cell QoS lower after, deny probe denied, trace rebuilt. Units in `check.py`: `detect`, `one_problem`, `fix_verified`, `critical_safe`, `swarm_deny`, `trace`. Each reads records keyed to the run id with wall-clock times that agree with CloudWatch.

- [ ] **Step 1: Failing tests:** one pass and one fail case for each new unit; `test_rebuild_follows_trace_from_problem_to_viavi_command`.
- [ ] **Step 2-4:** FAIL, implement, PASS; `python3 tools/rebuild.py` exits 0. **Step 5: Commit** `feat(track3): declared targets and checks for the autonomous run`.

### Task 11: Live end-to-end run

- [ ] **Step 1:** `deploy-ec2.sh`; apply pipeline, detectors, correlation, workflow; rebuild and redeploy runtimes (`agentcore/build_swarm.sh`, `aws/swarm_setup.py`, `aws/trigger_setup.py`).
- [ ] **Step 2:** On the EC2 run `~/team04-track3/viavi-run` (powercut scenario). Expected: Davis problem on S3 with S4/S1 correlated, workflow fires, swarm runs, S4 power raised, verified, phones without service down; a critical-cell deny and a stabilization deny appear.
- [ ] **Step 3:** Run `--pause`. Expected: capacity's model refused, run continues.
- [ ] **Step 4:** Confirm one trace id in CloudWatch, Dynatrace and Arize, service `multi-agent-swarm`. Save up to 20 run ids.
- [ ] **Step 5: Commit** `chore(track3): evidence from the live runs`.

### Task 12: Cleanup

**Files:** Delete `practice/` (all), `track3/adapters/replay.py`, `track3/rfmodel.py`, `track3/scenarios/{frozen-state.json,incident-v2-cutoff.json,faults}` if unreferenced, `track3/aws/tools_lambda.py` and its Gateway policy files, `cell-network-app/` only if the user confirms; trim tests that import them.

- [ ] **Step 1:** `grep -rn "replay\|rfmodel\|practice\|tools_lambda" --include='*.py' --include='*.sh' .` lists the references to remove. **Step 2:** delete in small commits, running the tests after each; keep `ReplayNetwork` until the twin tests no longer import it. **Step 3:** `python3 tools/rebuild.py` exits 0. **Step 4: Commit** `chore(track3): remove the old simulator and approval paths`.

### Task 13: Submission files

**Files:** Modify `track3/README.md` (first line the primary track, problem in three lines, new architecture, one command per stage, rebuild command, "Before Nov 1" note, credit the teammates' poller and pipeline), `track3/judging.json`, `track3/DATA.md` (Viavi scenarios, the organisers' `maveric_4cell` sample and its licence, critical-cell labels are synthetic), `track3/GAPS.md`.

- [ ] **Step 1:** `GAPS.md` entries, each with numbers or run ids: the cohort-lead conversation on human approval (date, role, outcome); the twin test is a model prediction; the Viavi MCP has no auth; causality rules if unavailable; simulated timestamps; the Dynatrace MCP state; critical-cell labels are synthetic.
- [ ] **Step 2:** `source ~/fyuz.env && aws s3 cp judging.json "s3://$BUCKET/judging/judging.json"`.
- [ ] **Step 3:** Record the demo from the frozen commit with commit and run id on screen. **Step 4: Commit and push** before 12:00 Nov 3 Seattle.
