# Governed SDLC Loop, Phase 1: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one measured turn of the loop real: verify an incident's outcome from Dynatrace, learn from scored runs, and promote an improved agent prompt through the same checkpoint that governs network changes.

**Architecture:** Everything is built around the existing `track3/checkpoint.py`. A new `PolicySet` lets the checkpoint hold two Cedar policy sets: the frozen network set (`track3.cedar`) and a new promotion set (`promotion.cedar`). New pure modules (`outcome.py`, `promotion.py`, `improve.py`) are unit-tested offline. Thin `track3.py` commands wire them to AWS, Dynatrace and Arize, and declare every run before it starts.

**Tech Stack:** Python 3.12, cedarpy, boto3, OpenTelemetry (Arize OTLP), dtctl (DQL), pytest via `uv`.

**Spec:** `docs/superpowers/specs/2026-11-02-governed-sdlc-loop-design.md`

## Global Constraints

- Never modify `track3/policies/track3.cedar`, `track3/scenarios/incident-v2-cutoff.json` or `track3/scenarios/frozen-state.json`. `tools/rebuild.py` checks their hashes against every declared run.
- Region `us-east-2`. Tables: `bb26-team04-track3-records`, `bb26-team04-track3-registry`, `bb26-team04-track3-approvals`. Log group `/bb26/team04/track3`. Bucket `bb26-p-team04-data-279760855394-use2`.
- No people's names in code, logs or records. Team code `team04`. Approvers are recorded as login + role (`ai_engineer`, `network_operator`).
- Judge model `us.anthropic.claude-haiku-5-5` (Anthropic). Agent model `openai.gpt-oss-120b-1:0`. No evaluator from the gpt-oss family.
- Every new run kind is declared with `declare()` before it starts and ends with a `run_end` record.
- Network decision records keep their current shape. A decision from another policy set adds `policy_set: "<name>"`.
- Tests run offline: `cd track3 && uv run --python 3.12 --with-requirements tests/requirements.txt pytest -q tests`

## Review Focus

- **No Dynatrace data after the fix** (simulator stopped, Grail delay): `verify` must report `inconclusive`, never `pass` or `fail`. Test in Task 2.
- **Arize unreachable when gathering promotion evidence:** `open_blocking_issues` becomes `-1` and the promotion must be denied (fail closed). Test in Task 3.
- **The approver declines, or approves with the wrong role:** recorded, then denied, and the registry is unchanged. Test in Task 3.
- **Old evidence after the policy split:** `tools/rebuild.py` reprints the same results for every existing run and exits 0. Test in Task 5.
- **An incident run with no executed fix** (declined, or the run failed): `verify` refuses with a clear message instead of scoring nothing. Test in Task 4.
- **A model reply with no JSON or no `affected_phones`:** F1 is 0.0 and the experiment continues. Test in Task 6.

---

### Task 1: Policy sets and `promotion.cedar` (WS-B)

**Files:**
- Create: `track3/policy.py`, `track3/policies/promotion.cedar`, `track3/tests/requirements.txt`, `track3/tests/conftest.py`, `track3/tests/test_policy.py`
- Modify: `track3/checkpoint.py` (`__init__`, `evaluate`, `decide`), `track3/agentcore/build.sh` (copy `policy.py` and `promotion.cedar`)

**Interfaces:**
- Produces: `PolicySet(path: Path)` with `.text: str`, `.sha256: str`, `.evaluate(principal: str, action: str, resource: str, context: dict, attrs: dict | None = None) -> tuple[str, list[str], list[str]]` returning `(decision, reasons, errors)`. When the decision is Deny with no reasons, reasons is `["no policy permits this call"]`. Reasons are the policies' `@id` names.
- Produces: `checkpoint.PROMOTION_POLICIES: Path`; `Checkpoint.policy_sets: dict[str, PolicySet]` with keys `"network"` and `"promotion"`; `Checkpoint.evaluate(..., policy_set: str = "network")` and `Checkpoint.decide(..., policy_set: str = "network")`.
- Produces: fixture `offline_cp(tmp_path)` in `conftest.py`. It returns a real `Checkpoint` with `boto3.client` patched to `MagicMock`, the Arize and Dynatrace exporters off (patch `checkpoint.ARIZE_KEY_FILE` to a missing path; unset `ARIZE_API_KEY`, `DT_TRACES_TOKEN`, `DT_PLATFORM_TOKEN`), and records written to `tmp_path`.

`promotion.cedar` holds exactly these policies (requests always carry all four context keys):

| `@id` | Effect | Condition (action `promote`) |
|---|---|---|
| `promote-needs-ai-engineer-and-evidence` | permit | `human_approved == true && approver_role == "ai_engineer" && eval_passed == true && open_blocking_issues == 0` |
| `agents-never-promote` | forbid | `principal is Agent` |
| `operators-cannot-approve-agent-changes` | forbid | `approver_role == "network_operator"` |
| `no-promotion-below-target` | forbid | `eval_passed == false` |
| `no-promotion-with-open-signal-issue` | forbid | `open_blocking_issues > 0` |
| `loop-never-touches-policy` | forbid | any action, `resource is Policy` |

- [ ] **Step 1: Write failing tests** in `tests/test_policy.py`:
  - `test_network_set_matches_frozen_scenario`: `restart_all_towers` gives Deny with reasons `["no-network-wide-restart"]`; `rollback_model` with `human_approved=False` gives Deny with `["no policy permits this call"]`; with `True`, Allow with `["rollback-needs-human"]`; `invoke_model` on `Model::"m"` with attrs `{"status": "withdrawn"}` gives Deny.
  - `test_promotion_allowed_for_ai_engineer_with_evidence`: `Loop::"improvement"`, `Prompt::"network-agent@v2"`, context `{human_approved: True, approver_role: "ai_engineer", eval_passed: True, open_blocking_issues: 0}` gives Allow with `["promote-needs-ai-engineer-and-evidence"]`.
  - `test_promotion_denials`: parametrized. Each case changes one input from the allowed case and expects Deny with that reason: principal `Agent::"network-agent"` → `agents-never-promote`; role `network_operator` → `operators-cannot-approve-agent-changes`; `eval_passed=False` → `no-promotion-below-target`; `open_blocking_issues=2` → `no-promotion-with-open-signal-issue`; `open_blocking_issues=-1` → `["no policy permits this call"]`; `human_approved=False` → `["no policy permits this call"]`; resource `Policy::"track3.cedar"` → `loop-never-touches-policy`.
  - `test_checkpoint_decide_records_policy_set(offline_cp)`: `offline_cp.decide(..., policy_set="promotion")` returns a record with `policy_set == "promotion"`. A network `decide` has no `policy_set` key.
- [ ] **Step 2: Run** `uv run ... pytest -q tests/test_policy.py`. Expected: FAIL (`No module named 'policy'`).
- [ ] **Step 3: Implement** `policy.py` by moving the existing body of `Checkpoint.evaluate` (the entity build, `cedarpy.is_authorized`, and the `policy0` → `@id` mapping) into `PolicySet.evaluate`. Write `promotion.cedar` with a header comment saying why it is a separate file. In `checkpoint.py`, add `PROMOTION_POLICIES = HERE / "policies" / "promotion.cedar"` and build `self.policy_sets`. Make `evaluate`/`decide` delegate to the named set, and keep `self.policies` and `self.policy_ids` for compatibility. In `build.sh`, copy `../policy.py` and `../policies/promotion.cedar`.
- [ ] **Step 4: Run tests.** Expected: all PASS.
- [ ] **Step 5: Commit** `feat(track3): Cedar policy sets; promotion.cedar for agent changes`

### Task 2: Outcome score (WS-A, pure)

**Files:**
- Create: `track3/outcome.py`, `track3/intents/coverage_first.json`, `track3/intents/throughput_first.json`, `track3/tests/test_outcome.py`

**Interfaces:**
- Produces: `load_intent(name: str) -> dict`
- Produces: `score_outcome(samples: list[dict], fix_time: datetime, problem_closed_at: datetime | None, intent: dict) -> dict`. `samples` are one-minute buckets `{"minute": datetime, "phones_dropped": int, "median_rate": float}`, sorted. The result has keys `restored`, `restored_at` (ISO or None), `problem_closed`, `no_regression`, `median_rate_after`, `score` (0..1, rounded to 3 places), `verdict` (`pass` | `fail` | `inconclusive`), `components` and `intent`.
- Produces: `label(judge_pass: bool, outcome_pass: bool) -> str`, returning `confirmed_good` | `sounded_right_failed` | `judge_miscalibrated` | `confirmed_bad`.

Intent values (from the spec):

| key | `coverage_first` | `throughput_first` |
|---|---|---|
| `clean_minutes` | 3 | 3 |
| `restore_within_minutes` | 6 | 6 |
| `close_within_minutes` | 10 | 10 |
| `median_rate_floor` | 1.0 | 2.0 |
| `weights` | restored 0.6, problem_closed 0.2, no_regression 0.2 | restored 0.2, problem_closed 0.2, no_regression 0.6 |
| `required` | `["restored", "no_regression"]` | `["no_regression"]` |

Rules:
- **Restored:** there is a start minute `t0` with `fix_time <= t0 <= fix_time + restore_within`, and the samples at `t0 .. t0 + clean_minutes - 1` all exist with `phones_dropped == 0`.
- **`median_rate_after`:** the mean `median_rate` over that clean window. If the network was not restored, it is the mean over all samples after the fix.
- **`no_regression`:** `median_rate_after >= median_rate_floor`.
- **`problem_closed`:** `problem_closed_at` is set and `<= fix_time + close_within`.
- **Score:** the sum of the weights whose component is true.
- **Verdict:** `pass` if every required component is true. `inconclusive` if it was not restored and the last sample is earlier than `fix_time + restore_within + clean_minutes - 1`. Otherwise `fail`.

- [ ] **Step 1: Write failing tests:**
  - `test_restored_within_window_passes`: dropped `[3, 0, 0, 0, 0]` at minutes 0..4 after the fix, rate 1.2, closed at +4 → `restored`, `verdict == "pass"`, `score == 1.0`.
  - `test_never_restored_fails`: dropped 2 for 10 minutes → `fail`, `restored` False.
  - `test_restored_too_late_is_not_restored`: zeros start at +7 → `restored` False.
  - `test_rate_below_floor_is_regression`: zeros, rate 0.5 → `no_regression` False, `fail` under `coverage_first`.
  - `test_short_data_is_inconclusive`: only 2 samples, both 0 → `inconclusive`.
  - `test_throughput_intent_weights_rate`: dropped 2 throughout, rate 3.5 → `pass` under `throughput_first`, `fail` under `coverage_first`. This shows the same data scored under two intents.
  - `test_label_table`: all four combinations.
- [ ] **Step 2: Run** them. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `outcome.py` and the two intent JSON files with the values above.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(track3): outcome score from Dynatrace data, intents for voice and video`

### Task 3: Prompt registry and governed promotion (WS-B)

**Files:**
- Create: `track3/promotion.py`, `track3/prompts/network-agent/v1.txt` (byte-identical to `incident.DIAGNOSE`), `track3/tests/test_promotion.py`
- Modify: `track3/tools/check.py` (units `promote`, `promote_denied`), `track3/track3.py` (commands `register-prompt`, `promote`)

**Interfaces:**
- Consumes: `Checkpoint.decide/evaluate(..., policy_set="promotion")` (Task 1).
- Produces: `PromptRegistry(ddb, table: str = "bb26-team04-track3-registry")` with:
  - `register(agent: str, version: str, text: str, who: str) -> dict`: item key `model_id = "prompt/<agent>@<version>"`, `status = "candidate"`, `sha256`, `text`
  - `get(agent, version) -> dict | None`: returns `agent`, `version`, `status`, `sha256`, `text`
  - `active(agent) -> str | None`: reads the pointer item `prompt/<agent>`, field `active`
  - `activate(agent, version, who) -> str | None`: sets the pointer, marks the version `approved` and the previous one `retired`, and returns the previous version
- Produces: `promote(cp, registry, agent: str, version: str, evidence: dict, approve, requested_by: str = 'Loop::"improvement"') -> dict`. `evidence` is `{"eval_passed": bool, "open_blocking_issues": int, "experiment_run_id": str}`. `approve(agent, version, evidence) -> (approver: str, role: str, ok: bool, note: str)`. It returns the last record, which is `promoted` or `denied`.
- Produces: `blocking_issue_count() -> int`, which returns `len(arize_signal.blocking())` or `-1` on any exception.

`promote()` follows the order of `Checkpoint.tool()`:
1. The candidate must exist with status `candidate`; otherwise it records `denied` with `["not a registered candidate"]`.
2. `decide` without approval (`approver_role: "none"`). If the reasons name a forbid policy, it records `denied` and stops.
3. Hypothetical `evaluate` with `human_approved=True, approver_role="ai_engineer"`. If that is Deny, it records `denied` with `["not allowed even with approval"]`.
4. `approval_requested` → `approve()` → an `approval` record with `approver`, `approver_role`, `approved`, `note`.
5. If declined, it records `denied` with `["person declined"]`.
6. Final `decide` with the real role. If Deny, it records `denied` with those reasons.
7. `registry.activate()` → a `promoted` record with `agent`, `version`, `previous`, `sha256`, `approver`, `approver_role`, `evidence`.

The resource is `Prompt::"<agent>@<version>"`.

- [ ] **Step 1: Write failing tests**, using `offline_cp` and an in-memory `FakeRegistry` with the same interface:
  - `test_ai_engineer_promotes_candidate`: kinds include `approval_requested`, `approval`, `decision` (Allow), `promoted` in that order; `registry.active("network-agent") == "v2"`.
  - `test_operator_approval_is_denied`: role `network_operator` → last record `denied` with `operators-cannot-approve-agent-changes`; the active version is unchanged.
  - `test_agent_cannot_promote_itself`: `requested_by='Agent::"network-agent"'` → `denied` with `agents-never-promote`, and `approve` is never called.
  - `test_below_target_denied_without_asking`: `eval_passed=False` → `denied` with `no-promotion-below-target`, and `approve` is never called.
  - `test_arize_unreachable_fails_closed`: `open_blocking_issues=-1` → `denied`, and the active version is unchanged.
  - `test_declined_is_recorded`: `ok=False` → `denied` with `["person declined"]`.
  - `test_check_units`: `check.promote` passes on the first test's records; `check.promote_denied` passes on the second's.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** `promotion.py` and the check units. In `track3.py`:
  - `register-prompt --agent A --version V` reads `prompts/A/V.txt`, declares units `["registry"]`, and registers the prompt.
  - `promote --agent A --version V --role R [--approve-as NAME] [--experiment-run ID] [--as-agent]` reads `eval_passed` from that experiment run's `experiment_result` record (`False` if missing). It declares units `["promote"]`, or `["promote_denied"]` when `--as-agent` is set or the role is not `ai_engineer`. The declaration includes `promotion_policies_sha256` and `candidate_sha256`. The approver is a person typing `yes` unless `--approve-as` is given, matching `human_approve`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Live check (WS-B owner; it writes official evidence, so agree on it first):** `./track3.sh register-prompt --agent network-agent --version v1`, then `promote ... --version v1 --role ai_engineer` typed by a person, then the same with `--role network_operator` for the live deny. Then `python3 tools/rebuild.py` should show `promote PASS` and `promote_denied PASS`.
- [ ] **Step 6: Commit** `feat(track3): governed promotion of agent prompts through the checkpoint`

### Task 4: `verify` run: Dynatrace outcome on the incident's trace (WS-A)

**Files:**
- Modify: `track3/outcome.py` (Dynatrace reads), `track3/track3.py` (command `verify`), `track3/checkpoint.py` (`_to_trace`: map `outcome` to `EVALUATOR`; add `label` to the copied attrs), `track3/tools/check.py` (unit `outcome`)
- Test: `track3/tests/test_outcome.py`

**Interfaces:**
- Consumes: `score_outcome`, `load_intent` (Task 2); `ops.dql` from `practice/agent.py`.
- Produces: `find_fix(records: list[dict]) -> dict | None`, which returns `{"fix_time": datetime, "problem": str | None, "trace_id": str, "run_id": str}` from the first `executed` record whose tool is `rollback_model`.
- Produces: `fetch_samples(start: datetime, end: datetime) -> list[dict]` and `problem_closed_at(display_id: str) -> datetime | None`.

The DQL mirrors the queries already proven in `practice/agent.py`:
`fetch logs, from: <start>, to: <end> | filter service.name == "mobile-env" and mobile.kind == "step" | summarize phones_dropped = max(toLong(mobile.phones_dropped)), median_rate = avg(toDouble(mobile.median_rate)), by: {minute = bin(timestamp, 1m)} | sort minute asc`
and
`fetch dt.davis.problems, from: now()-6h | filter display_id == "<id>" | sort timestamp desc | limit 1 | fields event.status, event.end`.
Validate both against tenant yqs73977 before wiring them in.

- [ ] **Step 1: Write failing tests:**
  - `test_find_fix_from_incident_records`: uses `evidence/runs/2026-11-01T19-20-track3-incident-01/records.jsonl` → the trace id matches the file and `fix_time` equals the `executed` record's time.
  - `test_find_fix_none_when_declined`: records with `denied` only → `None`.
  - `test_check_outcome_unit`: synthetic records `run_start`, `outcome` (verdict `pass`), `run_end` → PASS; with verdict `inconclusive` → FAIL with that word in the reason.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** `verify --run-id ID [--intent coverage_first] [--no-wait]`:
  - load the incident's local records, then `find_fix` (exit with "nothing to verify: no executed rollback in ID" if it returns None);
  - declare units `["outcome"]`, with the intent and `verifies_run_id`;
  - create `Checkpoint(new_run_id, incident_trace_id, ...)`;
  - wait until `fix_time + restore_within + clean_minutes + 1` minutes unless `--no-wait`;
  - query, score, record `outcome` with `verifies_run_id`, `problem`, the result fields, and `label` when the incident has a `judged` record;
  - `run_end`, then upload.
- [ ] **Step 4: Run tests.** Expected: PASS. **Live:** after a human-approved incident with the simulator running, `./track3.sh verify --run-id <id>`. The outcome span appears on the incident's trace in Arize.
- [ ] **Step 5: Commit** `feat(track3): verify runs score the real outcome from Dynatrace`

### Task 5: Evidence plumbing

**Files:**
- Modify: `track3/tools/rebuild.py`, `track3/judging.json`, `track3/GAPS.md`, `track3/DATA.md`, `track3/README.md`
- Test: `track3/tests/test_rebuild.py`

- [ ] **Step 1: Write the failing test.** `test_rebuild_keeps_old_results` runs `rebuild.main()`, expects return code 0, and checks that `evidence/results.json` equals the committed copy for every existing run id.
- [ ] **Step 2: Implement** in `rebuild.py`: when a declaration has `promotion_policies_sha256`, also check `policies/promotion.cedar`. Units come from `check.UNITS`, which now includes `outcome`, `promote`, `promote_denied` and `experiment`.
- [ ] **Step 3: Update docs.** `README.md`: one row per new command. `GAPS.md`: approver role is recorded, not IAM-enforced; AgentCore still runs prompt v1 until phase 2. `DATA.md`: prompts, datasets, exports. `judging.json`: new run ids after the live runs.
- [ ] **Step 4: Run all tests.** Expected: PASS. Then commit `chore(track3): rebuild and docs know the new run kinds`

### Task 6: Learn: dataset, experiment, Track 1 export (WS-C)

**Files:**
- Create: `track3/improve.py`, `track3/prompts/network-agent/v2.txt` (the candidate), `track3/tests/test_improve.py`
- Modify: `track3/track3.py` (commands `dataset`, `experiment`, `export`), `track3/tools/check.py` (unit `experiment`)

**Interfaces:**
- Consumes: `outcome.label` (Task 2); `PromptRegistry` (Task 3); `incident.parse_json`, `JUDGE`, `JUDGE_MODEL`.
- Produces: `affected_phones_f1(predicted: list[str], measured: list[str]) -> float`
- Produces: `build_dataset(evidence_dir: Path) -> list[dict]`. There is one item per incident run with a `judged` record. Each item has `run_id`, `trace_id`, `facts` (from `network_read`), `diagnosis` (the network agent's `model_call` output), `judge_score`, `outcome_verdict` (from a verify run with the same `trace_id`, or None), and `label` (None without an outcome).
- Produces: `export_track1(items: list[dict], system_prompt: str, out_dir: Path) -> tuple[Path, dict]`. It writes chat JSONL (`{"messages": [system, user, assistant]}`) from `confirmed_good` items only, plus `manifest.json` with `sha256`, `count`, `source_run_ids`, `purpose: "Track 1 SFT hand-off from Track 3 runs"`, `licence: "synthetic (mobile-env, MIT)"` and `created_at`.

The experiment:
- `experiment --agent network-agent --candidate v2 --repeats 3` declares targets before the run: candidate mean F1 ≥ current mean F1, candidate mean judge ≥ 4, and `frozen_state_sha256`.
- It runs the diagnose prompt for `current` (the registry's active version, or v1) and the candidate on `scenarios/frozen-state.json` through `cp.call_model`. Each is judged by `JUDGE_MODEL` and scored by F1 against the frozen `dropped_phones`.
- It records one `experiment_item` per call and one `experiment_result` with `candidate`, `current`, the means, the token totals and `eval_passed`.
- It mirrors the dataset and the experiment to Arize with the arize-dataset and arize-experiment skills.

- [ ] **Step 1: Write failing tests:** `test_f1` (exact match 1.0, disjoint 0.0, partial 0.667, empty prediction 0.0); `test_dataset_from_evidence` (on the committed evidence, every incident with a `judged` record yields an item, and its label is None without a verify run); `test_export_only_confirmed_good` (2 of 3 items are confirmed_good → 2 lines, and the manifest's sha256 matches the file); `test_parse_failure_scores_zero` (a reply with no JSON gives F1 0.0).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** Write `v2.txt` from the dataset's failures, or from Arize prompt optimization (arize-prompt-optimization skill). Keep the same JSON reply shape as v1.
- [ ] **Step 4: Run tests.** Expected: PASS. **Live:** `experiment` once, then `promote --experiment-run <id>` (Task 3).
- [ ] **Step 5: Commit** `feat(track3): dataset, experiment and Track 1 export`

### Task 7: Dynatrace Workflow starts the swarm (WS-D, spike first)

**Files:** `track3/dynatrace/workflow.json` (export), `track3/GAPS.md`

- [ ] **Step 1: Spike (time-box 60 min).** Can a Dynatrace Workflow call AWS from this account without access keys?
  - Option A: a Dynatrace AWS connection that assumes a role under `/bb26/` with `bb26-Boundary` and trust to Dynatrace.
  - Option B: API Gateway → Lambda, under a `/bb26/` role, which calls `invoke_agent_runtime` on `track3_agents`, with a shared secret in the Dynatrace credential vault.
  - Record the exact error for each option that fails.
- [ ] **Step 2:** If one works, the Workflow triggers on the problem `Cell network safety rule broken`. It sends the same payload `track3.py run` builds, with `operator: "dynatrace-workflow"`. The person still approves from the laptop through DynamoDB.
- [ ] **Step 3:** If neither works, add a dated GAPS.md entry with the request ids. Commit either way.

### Task 8: Tupl specialists behind the checkpoint (WS-E)

**Files:** `track3/advisors.py`, `track3/incident.py` (an optional `consult` step)

- [ ] **Step 1:** Write `consult(cp, specialist: str, question: str) -> dict`. It records a `handoff` decision (`Agent::"change-agent"` → `Agent::"<specialist>"`; the existing `read-and-handoff` policy permits handoffs) and calls `invoke_agent_runtime` on the Tupl runtime ARN from `bridge/children.json`. The reply goes through `cp.guard("OUTPUT", ...)` and is recorded as `advice`.
- [ ] **Step 2:** In `run_incident`, add an optional `advisors: list[str] = []` parameter. The change agent receives the advice in its prompt input. With the parameter empty, the behaviour is identical to today.
- [ ] **Step 3:** One local incident run with `--advisors tuplos_performance`. Commit.
