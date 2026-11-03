// Track 3 swarm policies: the orchestrator, three domain specialists (coverage, capacity, availability) and the risk
// critic. The checkpoint asks Cedar before every model call, hand-off, tool call, proposal and network change.
// The @id names are what the audit records show. track3.cedar (the two-agent incident) stays untouched.

// An agent may call a model only while the model registry says it is approved (the pause demo withdraws one).
@id("models-approved-only")
permit (principal, action == Action::"invoke_model", resource)
when { resource.status == "approved" };

// Only the orchestrator hands work out: to the three specialists and the risk critic.
@id("handoff-graph")
permit (principal == Agent::"orchestrator", action == Action::"handoff", resource)
when { [Agent::"coverage", Agent::"capacity", Agent::"availability", Agent::"risk"].contains(resource) };

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
  ["logs", "spans", "dt.davis.problems", "events"].contains(context.data_object) && context.has_write == false
};

// Each specialist may only propose changes in its own domain.
@id("domain-ownership")
permit (principal, action == Action::"propose", resource)
when {
  (principal == Agent::"coverage" && ["tx_power_dbm", "tilt_deg"].contains(context.parameter)) ||
  (principal == Agent::"capacity" && ["tx_power_dbm", "tilt_deg"].contains(context.parameter)) ||
  (principal == Agent::"availability" && ["enabled", "tx_power_dbm", "tilt_deg"].contains(context.parameter))
};

// Only the orchestrator asks a person, and reads the answer.
@id("approval-tools")
permit (principal == Agent::"orchestrator", action in [Action::"request_human_approval", Action::"get_approval_status"], resource);

// Only the orchestrator applies a change, and only one a person approved: same proposal hash, unexpired, unused.
@id("apply-approved")
permit (principal == Agent::"orchestrator", action == Action::"apply_cell_configuration", resource)
when { context.approval_valid == true };

// Only the orchestrator reverts, only after verification failed, and only back to the exact old value.
@id("revert-exact")
permit (principal == Agent::"orchestrator", action == Action::"revert_cell_configuration", resource)
when { context.ledger_status == "VERIFY_FAILED" && context.restores_old_value == true };

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
