// Track 3 promotion policies: changes to the agents themselves (a new prompt, model or runtime version).
// A separate file on purpose. track3.cedar governs the network and is frozen (its hash is in every declared run),
// and a policy change is its own class of change, owned by the risk owner, with no path from the improvement loop.
// Order matters only for naming: the checkpoint maps Cedar's policy0, policy1... to the @id below.
// Every promote request carries all four context keys: human_approved, approver_role, eval_passed, open_blocking_issues.

// An agent change goes live only when an AI engineer approves it and the evidence holds:
// the experiment met the target declared before it ran, and Arize Signal has no open critical or high issue.
@id("promote-needs-ai-engineer-and-evidence")
permit (principal, action == Action::"promote", resource)
when {
  context.human_approved == true &&
  context.approver_role == "ai_engineer" &&
  context.eval_passed == true &&
  context.open_blocking_issues == 0
};

// No self-approval: an agent never promotes anything, itself included.
@id("agents-never-promote")
forbid (principal is Agent, action == Action::"promote", resource);

// Separation of duties: network operators approve network changes, not changes to the agents.
@id("operators-cannot-approve-agent-changes")
forbid (principal, action == Action::"promote", resource)
when { context.approver_role == "network_operator" };

// A candidate that missed its declared target is never promoted, whoever approves it.
@id("no-promotion-below-target")
forbid (principal, action == Action::"promote", resource)
when { context.eval_passed == false };

// While Arize Signal has an open critical or high issue, nothing new goes live.
@id("no-promotion-with-open-signal-issue")
forbid (principal, action == Action::"promote", resource)
when { context.open_blocking_issues > 0 };

// Self-improvement can change how the swarm thinks, never what it is allowed to do.
@id("loop-never-touches-policy")
forbid (principal, action, resource is Policy);
