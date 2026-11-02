# Autonomous Network, Human-Approved Governance: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The swarm changes the network on its own inside a Cedar envelope. Changes to the governance layer (Cedar policy sets, the guardrail version) need a risk owner's hash-bound approval. Dynatrace (dtctl) reads are audited in our records only. The demo shows the network change happen, then traces the decision tree from the records.

**Architecture:** A new `change_control.py` mirrors `promotion.py`. It takes a candidate (policy text or guardrail version) through validation, invariants and an impact preview, then the risk owner's approval and a Cedar decision under `policies/governance.cedar`, then activation. Activation writes a content-addressed policy version and moves the active pointer in the registry table. Runtimes load the active policy by hash. The orchestrator drops the network approval step: it decides, applies to the replay network, verifies, and reverts if verification fails.

**Tech Stack:** Python 3.12, cedarpy (`validate_policies`, `is_authorized`), boto3 (DynamoDB, S3, Bedrock), dtctl, pytest.

**Spec:** `docs/superpowers/specs/2026-11-02-governed-sdlc-loop-design.md` §12 (and §5 for the promotion flow this mirrors).

## Global Constraints

- Deadline: the repo and bucket are final at **12:00 Nov 3 (Seattle)**; the account is read-only from 18:00 Nov 3.
- Never edit `policies/track3.cedar` or `policies/promotion.cedar`. `policies/swarm.cedar` changes only through the Task 3 flow (activation writes the new version); never hand-edit it.
- Envelope values (spec §12): power change ≤ **3** dB, tilt change ≤ **2**°, `enabled` only to **1**, at most **1** autonomous change per run. Existing forbids stay: power **30–46** dBm, tilt **0–10**°, protected cell **A**, **15**-minute stabilization.
- The approver role for governance is exactly `"risk_owner"`. The AI engineer's role stays `"ai_engineer"` (promotion only).
- Every Cedar `@id` named in this plan is copied verbatim into records and checks.
- Registry table `bb26-team04-track3-registry` (key `model_id`), bucket `bb26-p-team04-data-279760855394-use2`, prefix `governance/policies/`. No new tables.
- Offline tests never reach AWS, dtctl or a model: use `MagicMock` clients as `tests/conftest.py` does.
- Tests run with: `cd track3 && uv run --python 3.12 --with-requirements tests/requirements.txt pytest -q tests`.
- Old declared runs must still rebuild: `python3 tools/rebuild.py` exits 0 at the end of every task.

## Review Focus

- A candidate with a Cedar **syntax error** must be refused before approval, with the parser's message recorded. Never activate it, never fail open. → Task 3 `test_candidate_with_syntax_error_is_refused`.
- An approval whose **hash doesn't match the candidate** (the file was edited after the preview) must deny the activation. → Task 3 `test_activation_denied_when_hash_changed`.
- A runtime that finds an **S3 policy body whose sha256 differs from the pointer** must refuse to start, not fall back to the bundled file. → Task 4 `test_load_active_refuses_tampered_body`.
- An apply decided under the **old (approval) policy** must come out as a clean Deny ("no policy permits this call"), not a Cedar evaluation error. The context therefore always carries `approval_valid`. → Task 5 `test_old_policy_denies_autonomous_apply_cleanly`.
- A **second autonomous change in the same run** (e.g. after a revert) must be denied by `one-autonomous-change-per-run`; the revert itself must stay allowed. → Task 2 `test_second_apply_in_run_denied_revert_allowed`.

---

### Task 1: Policy versions by hash, and rebuild by hash

**Files:**
- Create: `track3/policies/versions/` (with `af0caba6…` = current `swarm.cedar` copied as `<sha256>.cedar`)
- Modify: `track3/tools/rebuild.py:39-41`
- Test: `track3/tests/test_rebuild.py`

**Interfaces:**
- Produces: `tools/rebuild.py: policy_file(decl: dict) -> Path`. Returns `policies/versions/<decl["policies_sha256"]>.cedar` when it exists, else `ROOT / decl.get("policies_path", "policies/track3.cedar")`.

- [ ] **Step 1: Write failing tests.** `test_policy_file_prefers_version_by_hash`: a decl whose sha matches a file in `versions/` resolves to it even when `policies_path` points to a changed file. `test_policy_file_falls_back_to_path`: no version file → `policies_path`.
- [ ] **Step 2: Run them; expect FAIL** (`policy_file` not defined).
- [ ] **Step 3: Implement `policy_file`** and use it in the hash check. Copy the current `policies/swarm.cedar` and `policies/track3.cedar` into `policies/versions/<sha256>.cedar`.
- [ ] **Step 4: Run tests and `python3 tools/rebuild.py`; expect PASS and exit 0.**
- [ ] **Step 5: Commit** `feat(track3): store policy versions by hash; rebuild resolves by hash`.

### Task 2: The autonomous envelope policy — DONE (2026-11-02)

Built: `policies/swarm-autonomous.cedar` + `.cedarschema`, `policies/governance.cedar` + `.cedarschema`, `tests/test_autonomous_policy.py` (43 pass). Decision-tree actions `select_proposal` and `request_governance_change` added; contexts carry `proposal_id`. The steps below are kept for reference.

#### (original task)

**Files:**
- Create: `track3/policies/candidates/swarm-autonomous.cedar` (a copy of `swarm.cedar` with the changes below)
- Test: `track3/tests/test_autonomous_policy.py`

**Interfaces:**
- Produces: the apply context contract that Task 5 sends. Keys: `parameter: str, old_value: int, new_value: int, abs_delta: int, risk_checks_passed: bool, applies_this_run: int, minutes_since_last_change: int, approval_valid: bool`.

Changes from `swarm.cedar`:
- Remove `apply-approved` and `approval-tools`.
- Add `@id("apply-autonomous-within-envelope")`: permit orchestrator `apply_cell_configuration` when `risk_checks_passed` and (`tx_power_dbm` with `abs_delta <= 3`, or `tilt_deg` with `abs_delta <= 2`, or `enabled` with `new_value == 1`).
- Add `@id("one-autonomous-change-per-run")`: forbid `apply_cell_configuration` when `applies_this_run >= 1`.
- Add `@id("request-governance-change")`: permit orchestrator `request_governance_change`.
- Keep `revert-exact`, `power-range`, `tilt-range`, `protected-cell`, `stabilization-15m` unchanged.

- [ ] **Step 1: Write failing tests** with `cedar_decide` (from `agents.governance`) on the candidate text:
  - outage-C fix: `C enabled 0→1`, checks passed → Allow, by `apply-autonomous-within-envelope`.
  - `tx_power_dbm` 40→44 (delta 4) → Deny, `no policy permits this call`.
  - `tx_power_dbm` 40→43 → Allow; 44→47 → Deny by `power-range`.
  - cell A within envelope → Deny by `protected-cell`.
  - `risk_checks_passed: false` → Deny.
  - `minutes_since_last_change: 0` → Deny by `stabilization-15m`.
  - `test_second_apply_in_run_denied_revert_allowed`: `applies_this_run: 1` apply → Deny by `one-autonomous-change-per-run`; `revert_cell_configuration` with `ledger_status: "VERIFY_FAILED", restores_old_value: true` → Allow.
  - `request_human_approval` → Deny.
- [ ] **Step 2: Run; expect FAIL** (file missing).
- [ ] **Step 3: Write the candidate file.**
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat(track3): candidate swarm policy: autonomous changes inside an envelope`.

### Task 3: Governance policy set and the policy-change flow

**Files:**
- Create: `track3/policies/invariants.json`, `track3/change_control.py` (`governance.cedar` exists; `validate` uses `cedarpy.validate_policies` against the set's `.cedarschema`)
- Modify: `track3/checkpoint.py:97` (add `"governance": PolicySet(GOVERNANCE_POLICIES)`), `track3/agents/governance.py:70` (`SwarmCheckpoint.evaluate`: when `policy_set` is not `"network"`, defer to `Checkpoint.evaluate`)
- Test: `track3/tests/test_change_control.py`

**Interfaces:**
- Consumes: `PolicySet` (`policy.py`), `PromptRegistry._get/_put` pattern (`promotion.py`), `cedar_decide` (`agents/governance.py`).
- Produces:
  - `class GovernanceRegistry(ddb, s3)` with `active_policy(set_name: str) -> str | None` (sha), `activate_policy(set_name: str, sha: str, text: str, who: str) -> str | None` (returns the previous sha), `active_guardrail() -> str | None`, `activate_guardrail(version: str, who: str) -> str | None`. Keys: `policy/<set>` pointer `{status: "pointer", active: <sha>}`, `policy/<set>@<sha>` `{status, sha256, s3_key}`, `guardrail/track3` pointer. `activate_policy` uploads `governance/policies/<sha>.cedar` and writes `policies/versions/<sha>.cedar` locally.
  - `validate(text: str) -> list[str]`: errors, empty when OK. Uses `cedarpy.validate_policies` with no schema, and catches parse errors.
  - `check_invariants(text: str, cases: list[dict]) -> list[dict]`: the failing cases.
  - `impact(text: str, records: list[dict]) -> list[dict]`: re-evaluates every `kind == "decision"` record that has no `policy_set` (swarm decisions) under the candidate. Returns flips as `{run_id, seq, action, resource, was, now, policies}`.
  - `policy_change(cp, registry, set_name: str, candidate: Path, records: list, approve, requested_by: str) -> dict`: returns the last record, `policy_activated` or `denied`.
- `governance.cedar` rules (`@id` exact):
  - `governance-change-needs-risk-owner-and-evidence`: permit `activate_policy`/`activate_guardrail` when `human_approved && approver_role == "risk_owner" && checks_passed && hash_matches`.
  - `agents-never-change-governance`: forbid `principal is Agent` on `activate_policy`/`activate_guardrail`.
  - `only-risk-owner-approves-governance`: forbid when `approver_role != "risk_owner" && human_approved`.
  - `no-change-with-failed-checks`: forbid when `checks_passed == false`.
  - `governance-is-not-self-amending`: forbid `activate_policy` on `Policy::"governance"`.
  - Every activate request carries all four context keys.
- `invariants.json`: a list of `{name, principal, action, resource, context, attrs, expect, by}`. At least: power 50 → Deny `power-range`; cell A → Deny `protected-cell`; minutes 0 → Deny `stabilization-15m`; a withdrawn model → Deny; `dt_query` with `has_write: true` → Deny; `handoff` from coverage → Deny.

Flow order in `policy_change` (same shape as `promotion.promote`):
1. Record `governance_candidate` (sha, set, diff line counts).
2. Deny probe: `cp.decide('Agent::"orchestrator"', "activate_policy", ...)` must be Deny by `agents-never-change-governance`; record `governance_deny_probe`.
3. Validate, check invariants, run the impact preview; record `governance_checks` (errors, failed invariants, flips).
4. Record `approval_requested` with `candidate_sha256`.
5. `approve(req) -> (approver, role, ok, note, sha_seen)`.
6. Record `approval`.
7. Run the final `cp.decide(..., policy_set="governance")` with `hash_matches = sha_seen == candidate sha and file re-hash unchanged`.
8. `registry.activate_policy`.
9. Record `policy_activated {set, previous_sha256, sha256, approver, approver_role}`.

- [ ] **Step 1: Write failing tests** (MagicMock ddb/s3, `offline_cp`):
  - `test_risk_owner_activates_candidate`: last record is `policy_activated` and the pointer moves.
  - `test_agent_probe_denied`: the `governance_deny_probe` record is Deny by `agents-never-change-governance`.
  - `test_ai_engineer_approval_denied`: Deny by `only-risk-owner-approves-governance`.
  - `test_candidate_with_syntax_error_is_refused`: `governance_checks.errors` is non-empty, denied by `no-change-with-failed-checks`, `approve` never called.
  - `test_invariant_break_refused`: a candidate without `protected-cell` → denied.
  - `test_activation_denied_when_hash_changed`: `approve` returns a different sha → denied.
  - `test_impact_lists_flips`: on `evidence/runs/2026-11-02T12-58-track3-swarm-01/records.jsonl`, the candidate flips the `apply_cell_configuration` Allow (approval-based) to Deny or error.
  - `test_governance_policy_not_self_amending`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement `governance.cedar`, `invariants.json`, `change_control.py`, and the two policy-set hooks.**
- [ ] **Step 4: Run all tests and rebuild; expect PASS and exit 0.**
- [ ] **Step 5: Commit** `feat(track3): governance policy set and risk-owner policy changes`.

### Task 4: Runtimes load the active policy by hash

**Files:**
- Modify: `track3/agents/governance.py:51-56` (`SwarmCheckpoint.__init__`), `track3/aws/swarm_setup.py:27-40` (runtime role gets `s3:GetObject` on `governance/policies/*`), `track3/swarm.py:74,181` and `track3/agents/cloud.py:130` (declaration and `run_start` carry the active sha and `policies_path: "policies/versions/<sha>.cedar"`)
- Test: `track3/tests/test_change_control.py`

**Interfaces:**
- Consumes: `GovernanceRegistry.active_policy` (Task 3).
- Produces: `change_control.load_active(set_name: str, ddb, s3, fallback: Path) -> tuple[str, str]` returning `(text, sha256)`. No pointer → fallback file. Pointer set → S3 body, and raises `RuntimeError` if the body's sha256 ≠ the pointer. `SwarmCheckpoint.swarm_policy_sha` is the sha in use.

- [ ] **Step 1: Write failing tests:** `test_load_active_uses_pointer`, `test_load_active_falls_back_without_pointer`, `test_load_active_refuses_tampered_body`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement**, and wire the `SwarmCheckpoint`, declaration and `run_start` fields.
- [ ] **Step 4: Run tests and rebuild; expect PASS.**
- [ ] **Step 5: Commit** `feat(track3): runtimes run the active policy version, checked by hash`.

### Task 5: The orchestrator applies, verifies and reverts on its own

**Files:**
- Modify: `track3/adapters/replay.py` (add `apply`), `track3/agents/orchestrator.py:115-121` (`apply_context`), `:254-284` (replace `ask_person` with `apply_autonomously`), `:235-252` (`deny_probe` uses the new context), `:309-380` (`run_incident` steps 6–7), `track3/swarm.py:93-120` (`short()` lines for the new records), `track3/tools/check.py` (new units)
- Test: `track3/tests/test_swarm.py`

**Interfaces:**
- Consumes: the Task 2 context contract; `risk_checks.check(...)["recommendation"] == "approval_required"` means the checks passed (keep that value; the context key is `risk_checks_passed`).
- Produces:
  - `ReplayNetwork.apply(cell_id: str, parameter: str, value: int, by: str) -> dict`: updates `config`, appends to the ledger `{cell_id, parameter, old_value, new_value, by, minutes_ago: 0}`, recomputes `snapshot` (`config_version + 1`), and returns the new snapshot's network summary.
  - `apply_context(net, cell_id, parameter, value, risk_checks_passed: bool, applies_this_run: int, minutes=None) -> dict`, with all Task 2 keys and `approval_valid: False` always.
  - `apply_autonomously(run, chosen: dict, say) -> dict` returns the outcome.
  - Records:
    - `applied {proposal_id, cell_id, parameter, old_value, new_value, policies, policy_sha256}`;
    - `verified {ok, phones_without_service_before, phones_without_service_after, predicted_after}`, where ok means after < before and after ≤ predicted_after;
    - `reverted {cell_id, parameter, restored_value, policies}`;
    - `escalated {proposal_id, policies}`, after a Deny, preceded by a `request_governance_change` decision.
  - Units in `check.py`:
    - `swarm_autonomous_apply`: an Allow by `apply-autonomous-within-envelope`, then `applied` with `policy_sha256 == run_start.policies_sha256`, and no `approval_requested` for the network.
    - `swarm_verify`: `verified.ok`, or a failed `verified` followed by `reverted`.
    - `governance_change`, `governance_denied` for Task 3 runs.
  - Keep `swarm_allow` for old runs.

- [ ] **Step 1: Write failing tests:**
  - `test_replay_apply_restores_outage_C`: C enabled→1 lowers phones without service.
  - `test_apply_context_keys`: exactly the Task 2 keys.
  - `test_old_policy_denies_autonomous_apply_cleanly`: under `swarm.cedar` the decision is Deny with no errors.
  - `test_check_autonomous_apply_pass_and_fail`: built record lists.
  - `test_check_verify_revert_path`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** The stabilization probe stays: after `applied`, `minutes=0` must Deny by `stabilization-15m` (or `one-autonomous-change-per-run`; accept either in `swarm_stabilization`).
- [ ] **Step 4: Run all tests and rebuild; expect PASS.** Then do a dev run, `python swarm.py run --fault outage-C`: under the old policy it ends `apply denied` and `escalated`.
- [ ] **Step 5: Commit** `feat(track3): the swarm applies, verifies and reverts inside the envelope`.

### Task 6: Guardrail version through the same gate

**Files:**
- Create: `track3/scenarios/guardrail-cases.json` (`{"blocked": [INJECTION from track3.py:387, plus 2 more], "allowed": [3 normal tool results from a recorded run]}`)
- Modify: `track3/change_control.py` (add `guardrail_change`), `track3/checkpoint.py:32-33,188-195` (version from `GovernanceRegistry.active_guardrail()`, fallback `"DRAFT"`; every `guardrail` record carries `guardrail_version`; `run_start` carries it)
- Test: `track3/tests/test_change_control.py`

**Interfaces:**
- Produces: `guardrail_change(cp, registry, version: str, cases: dict, approve, requested_by: str) -> dict`, the same flow as `policy_change`. Checks: every `blocked` text gives `GUARDRAIL_INTERVENED` and every `allowed` text gives `NONE`, using `apply_guardrail(guardrailVersion=version)`. The last record is `guardrail_activated {previous_version, version, approver, approver_role}` or `denied`.

- [ ] **Step 1: Write failing tests** (mocked `cp.runtime.apply_guardrail`): `test_guardrail_activated_by_risk_owner`, `test_guardrail_refused_when_injection_passes`, `test_guard_records_version`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** Create the version once, by hand: `aws bedrock create-guardrail-version --guardrail-identifier lr4zff1274hc --region us-east-2`.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat(track3): guardrail versions activated only by a risk owner`.

### Task 7: Dynatrace reads work on AgentCore

**Files:**
- Modify: `track3/agentcore/build_swarm.sh` (bundle the linux-arm64 `dtctl` binary and the Dynatrace skills), `track3/agents/governance.py:74-97` (`tool_call` gets `dql_sha256` when the result has `dql`)
- Test: `track3/tests/test_swarm.py` `test_tool_call_records_dql_sha`

No pairing with Dynatrace's query log: the records are the audit. Manual ops step: put a read-only Dynatrace token in secret `team04/track3`.

- [ ] Write the failing test, run it (FAIL), implement, run it (PASS), commit `feat(track3): dtctl on AgentCore, DQL hash in each tool_call`.

### Task 7b: Decision tree view

**Files:**
- Create: `track3/tools/decision_tree.py --run-id <id> [--from-s3]`
- Test: `track3/tests/test_decision_tree.py`

**Interfaces:** `tree(records: list) -> dict`. Groups `decision` records by `context.proposal_id` (no id: run-level nodes), in seq order. Each node is `{seq, action, principal, resource, decision, policies}`, with the `applied`/`verified`/`reverted`/`escalated` records attached under their proposal. Prints one indented tree per proposal, with the deciding `@id` on each line. It reads only local or S3 records, so it doubles as a read-only live test.

- [ ] Test `test_tree_groups_by_proposal_in_seq_order` on a built record list; implement; commit `feat(track3): decision tree view of a run`.

### Task 8: Commands, declared runs, and the write-up

**Files:**
- Modify: `track3/swarm.py:252-267` (sub-commands `policy-change --set swarm --file <candidate> --role risk_owner [--as-agent]` and `guardrail-change --version N --role risk_owner`, declared, with units `governance_change` and `governance_denied`; `run` declares `swarm_autonomous_apply`, `swarm_verify` instead of `swarm_allow`), `track3/README.md`, `track3/GAPS.md`, `track3/judging.json`, `track3/agentcore` rebuild plus `aws/swarm_setup.py` redeploy
- Test: `track3/tests/test_rebuild.py` (`test_rebuild_runs_new_units`)

**The demo sequence** (each step a declared run; ids go in `judging.json`):
1. `./swarm.sh run --cloud --declare --fault outage-C` under the old policy: apply denied, escalated.
2. `./swarm.sh policy-change --set swarm --file policies/swarm-autonomous.cedar --role risk_owner`: agent probe denied, impact preview shown, a person types `yes`, `policy_activated`.
3. `./swarm.sh run --cloud --declare --fault outage-C --dynatrace`: the network change happens (tower C back on), verified, stabilization deny; then `python3 tools/decision_tree.py --run-id <id>` walks the tree.
4. `./swarm.sh guardrail-change --version 1 --role risk_owner`.
5. `--pause` run on AgentCore.

- [ ] **Step 1: Write the failing rebuild test; run; expect FAIL.**
- [ ] **Step 2: Implement the commands and declarations.**
- [ ] **Step 3: Update docs.** README: one command per stage and the §12 decision. GAPS.md: the guide line vs the judge's guidance; Gateway Cedar for the swarm tools; one IAM role per agent; approver role not enforced by IAM.
- [ ] **Step 4: Run all tests and rebuild; expect PASS and exit 0.**
- [ ] **Step 5: Commit** `feat(track3): governance commands, declared units, docs`.
- [ ] **Step 6: Rebuild and redeploy the runtimes** (`agentcore/build_swarm.sh`, `python aws/swarm_setup.py`), then record demo steps 1–5. Copy `judging.json` to `s3://$BUCKET/judging/`.
