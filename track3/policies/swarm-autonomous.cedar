// Track 3 swarm policies, autonomous version: the network changes without a person, inside an envelope.
// People change the envelope (policies/governance.cedar, risk owner); agents never do.
// Every branch of the swarm's decision tree is its own Cedar action, so a run's decision records ARE its tree:
//   kpi_snapshot -> handoff -> (read tools) -> propose -> run_risk_checks -> select_proposal
//     -> apply_cell_configuration -> (verify) -> revert_cell_configuration | hold
//     -> request_governance_change (when the fix is outside the envelope)
// Each context carries proposal_id, so the records link one proposal from proposal to apply to revert.
// Validated against swarm-autonomous.cedarschema. The @id names are what the audit records show; every policy has one,
// because the checkpoint maps Cedar's policy0, policy1... to them in file order.
// swarm.cedar (the human-approval version) stays untouched: its hash is in declared runs.

// ---- who may think, and with which model ----------------------------------------------------------------------------

// An agent may call a model only while the model registry says it is approved (the pause demo withdraws one).
@id("models-approved-only")
permit (principal, action == Action::"invoke_model", resource)
when { resource.status == "approved" };

// Only the orchestrator hands work out: to the three specialists and the risk critic.
@id("handoff-graph")
permit (principal == Agent::"orchestrator", action == Action::"handoff", resource)
when { [Agent::"coverage", Agent::"capacity", Agent::"availability", Agent::"risk"].contains(resource) };

// ---- reading the network changes nothing -----------------------------------------------------------------------------

// Reading the network, simulating a change, running the risk checks and reading a skill change nothing.
@id("read-tools")
permit (
  principal,
  action in [Action::"get_cell_state", Action::"get_neighbor_state", Action::"get_current_configuration",
             Action::"get_recent_alarm_history", Action::"dt_log_patterns", Action::"dt_problems", Action::"dt_spans",
             Action::"load_skill", Action::"simulate_power_change", Action::"simulate_tilt_change",
             Action::"simulate_cell_state", Action::"estimate_neighbor_impact", Action::"run_risk_checks"],
  resource
);

// The KPI snapshot is deterministic and read-only.
@id("snapshot-tool")
permit (principal, action == Action::"kpi_snapshot", resource);

// A free-form DQL query only reads logs, spans, events or problems, at most 2 hours back. The checkpoint parses the
// query itself: the model does not write these context values.
@id("dql-read-shape")
permit (principal, action == Action::"dt_query", resource)
when {
  ["fetch", "timeseries", "smartscapeNodes"].contains(context.dql_verb) && context.window_minutes <= 120 &&
  ["logs", "spans", "dt.davis.problems", "events"].contains(context.data_object) && !context.has_write
};

// ---- the decision tree -----------------------------------------------------------------------------------------------

// Node 1, propose: each specialist may only propose changes in its own domain.
@id("domain-ownership")
permit (principal, action == Action::"propose", resource)
when {
  (principal == Agent::"coverage" && ["tx_power_dbm", "tilt_deg"].contains(context.parameter)) ||
  (principal == Agent::"capacity" && ["tx_power_dbm", "tilt_deg"].contains(context.parameter)) ||
  (principal == Agent::"availability" && ["enabled", "tx_power_dbm", "tilt_deg"].contains(context.parameter))
};

// Node 2, select: the orchestrator may act on a proposal only if code's risk checks passed and the risk critic
// agreed and did not rate it high risk.
@id("select-eligible-only")
permit (principal == Agent::"orchestrator", action == Action::"select_proposal", resource)
when { context.risk_checks_passed && context.critic_agrees && context.risk != "high" };

// Node 3, apply: the orchestrator applies the proposal it selected, with no person, when the change is small.
// Power at most 3 dB, tilt at most 2 degrees, or switching a cell back on. Anything bigger goes to node 5.
@id("apply-autonomous-within-envelope")
permit (principal == Agent::"orchestrator", action == Action::"apply_cell_configuration", resource)
when {
  context.selected_proposal_matches && context.risk_checks_passed &&
  ((context.parameter == "tx_power_dbm" && context.abs_delta <= 3) ||
   (context.parameter == "tilt_deg" && context.abs_delta <= 2) ||
   (context.parameter == "enabled" && context.new_value == 1))
};

// Node 4, revert: only after verification failed, and only back to the exact old value.
@id("revert-exact")
permit (principal == Agent::"orchestrator", action == Action::"revert_cell_configuration", resource)
when { context.ledger_status == "VERIFY_FAILED" && context.restores_old_value };

// Node 5, escalate: when the fix is outside the envelope, the orchestrator may ask the risk owner to change the policy.
// Asking is all it can do: governance.cedar forbids every agent from activating a policy.
@id("escalate-to-governance")
permit (principal == Agent::"orchestrator", action == Action::"request_governance_change", resource is Policy);

// ---- never, whatever the envelope says ---------------------------------------------------------------------------------

// Never: transmit power outside 30-46 dBm.
@id("power-range")
forbid (principal, action == Action::"apply_cell_configuration", resource)
when { context.parameter == "tx_power_dbm" && (context.new_value > 46 || context.new_value < 30) };

// Never: tilt outside 0-10 degrees.
@id("tilt-range")
forbid (principal, action == Action::"apply_cell_configuration", resource)
when { context.parameter == "tilt_deg" && (context.new_value > 10 || context.new_value < 0) };

// Never: an agent change to cell A, which serves emergency services.
@id("protected-cell")
forbid (principal, action in [Action::"apply_cell_configuration", Action::"revert_cell_configuration"], resource == Cell::"A");

// Never: a second agent change to a cell within 15 minutes of the last one. Operator fault injections don't count.
@id("stabilization-15m")
forbid (principal, action == Action::"apply_cell_configuration", resource)
when { context.minutes_since_last_change < 15 };

// Never: more than one autonomous change in a run. A revert is not a change and stays allowed.
@id("one-autonomous-change-per-run")
forbid (principal, action == Action::"apply_cell_configuration", resource)
when { context.applies_this_run >= 1 };
