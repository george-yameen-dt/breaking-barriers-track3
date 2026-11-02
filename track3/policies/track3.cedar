// Track 3 checkpoint policies. Every model, tool and agent-to-agent call is checked against these.
// Order matters only for naming: the checkpoint maps Cedar's policy0, policy1... to the @id below.

// An agent may call a model only while the model registry says it is approved.
@id("models-approved-only")
permit (principal, action == Action::"invoke_model", resource)
when { resource.status == "approved" };

// Agents may read the network state and hand work to each other.
@id("read-and-handoff")
permit (principal, action in [Action::"read_network", Action::"handoff"], resource);

// Rolling the radio model back is allowed, but only after a person approves it.
@id("rollback-needs-human")
permit (principal, action == Action::"rollback_model", resource)
when { context.human_approved == true };

// Changing one tower's power is allowed after a person approves it; never more than one tower at a time.
@id("one-tower-with-human")
permit (principal, action == Action::"set_tower_power", resource)
when { context.human_approved == true && context.towers <= 1 };

// Never: an agent restarting every tower in the network.
@id("no-network-wide-restart")
forbid (principal, action == Action::"restart_all_towers", resource);
