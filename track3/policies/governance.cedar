// Track 3 governance policies: changes to the governance layer itself (a Cedar policy set, the guardrail version).
// The network runs autonomously inside the active policy; this is the one place a person must say yes.
// Approvals are bound to the candidate's sha256, so what is activated is exactly what the risk owner reviewed.
// Every activate request carries all four context keys: human_approved, approver_role, checks_passed, hash_matches.
// checks_passed means: the candidate validates against its schema, the invariants still hold, and the impact preview ran.
// This file is changed only by code review in Git: governance-is-not-self-amending forbids the flow from touching it.
// Validated against governance.cedarschema.

// A governance change goes live only when a risk owner approves the exact candidate and its checks passed.
@id("governance-change-needs-risk-owner-and-evidence")
permit (principal is Person, action in [Action::"activate_policy", Action::"activate_guardrail"], resource)
when { context.human_approved && context.approver_role == "risk_owner" && context.checks_passed && context.hash_matches };

// No self-governance: an agent never activates a policy or a guardrail, its own envelope least of all.
@id("agents-never-change-governance")
forbid (principal is Agent, action in [Action::"activate_policy", Action::"activate_guardrail"], resource);

// Separation of duties: an AI engineer promotes agents, a network operator runs the network; neither changes the rules.
@id("only-risk-owner-approves-governance")
forbid (principal, action in [Action::"activate_policy", Action::"activate_guardrail"], resource)
when { context.human_approved && context.approver_role != "risk_owner" };

// A candidate that fails validation or breaks an invariant is never activated, whoever approves it.
@id("no-change-with-failed-checks")
forbid (principal, action in [Action::"activate_policy", Action::"activate_guardrail"], resource)
when { !context.checks_passed };

// The approval covers one exact candidate: if the file changed after the person saw it, the approval is void.
@id("approval-bound-to-candidate-hash")
forbid (principal, action in [Action::"activate_policy", Action::"activate_guardrail"], resource)
when { context.human_approved && !context.hash_matches };

// The governance rules cannot be changed through the governance flow: only by code review in Git.
@id("governance-is-not-self-amending")
forbid (principal, action == Action::"activate_policy", resource == Policy::"governance");
