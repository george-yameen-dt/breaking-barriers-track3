# The policies, explained

**One idea:** the agents may change a mobile network on their own, but only inside limits that a policy states in plain rules, and only a named person can change those limits. Everything is written in Cedar (AWS's policy language), decided by one checkpoint, and recorded.

Three layers, three files, 36 rules in all:

| Layer | File | Question it answers | Who is in charge |
|---|---|---|---|
| 1. The swarm | `policies/swarm.cedar` (24 rules) | What may the agents do, on their own, to the network? | nobody: the rules decide |
| 2. The rules | `policies/governance.cedar` (6 rules) | Who may change those rules? | a **risk owner**, for one exact file |
| 3. The agents | `policies/promotion.cedar` (6 rules) | Who may change the agents themselves? | an **AI engineer**, with evidence |

**See it run, with no setup:** `python3 tools/demo_policies.py` (27 real decisions against the real files; add `--act 1|2|3` for one layer, `--step` to pause between scenarios). A test makes sure everything below is true.

Cedar's rule of thumb: nothing is allowed unless a `permit` says so, and any matching `forbid` wins. So the "permit" rules say what the swarm *can* do, and the "forbid" rules are the *never* list.

---

## Layer 1: what the swarm may do alone (`swarm.cedar`)

### Who and what is allowed to act
| Rule | In plain words |
|---|---|
| `models-approved-only` | An agent may call a model only while the registry says the model is approved. **Withdraw a model and its calls are refused (the pause demo).** |
| `handoff-graph` | Only the orchestrator hands work to the three specialists and the risk critic. Nobody else delegates. |
| `read-tools` | Reading the network, simulating a change, running the risk checks: these change nothing, so they are open. |
| `snapshot-tool` | The KPI snapshot is a read-only fact, the same for every agent. |
| `dql-read-shape` | A free-form query to Dynatrace may only read (logs, spans, events, problems), at most two hours back. |
| `tools-approved-only` | **Nothing is callable until it is registered and approved.** An unapproved tool is refused before any other rule is considered. |
| `viavi-read` | The simulator's read calls are on a short list; starting, stopping or editing a scenario is not on it. |
| `viavi-command-allowlist` | The only write the agents have: the orchestrator may send `tx_power` or `antenna_tilt`. `raw`, `turn_off`, handovers and the rest do not exist for them. |
| `viavi-trace-id-in-reason` | Every command to the simulator carries the run's trace id, so its event log matches our audit record. No trace id, no command. |
| `viavi-delay-short` | A command applies now (or within a minute), never scheduled for later. |

### The decision tree: propose, select, apply, undo
| Rule | In plain words |
|---|---|
| `domain-ownership` | Each specialist proposes only changes in its own area. |
| `select-eligible-only` | The orchestrator may act only on a proposal that passed the code's risk checks and that the critic agreed with and did not rate high-risk. |
| `apply-autonomous-within-envelope` | **The autonomous fix.** The orchestrator applies the selected proposal with no person, when it is small: power from 6 dB down to 8 dB up, tilt at most 5 degrees. Anything bigger is not allowed alone. |
| `revert-exact` | A change that did not help is undone, but only back to the exact old value, and only after a failed check. |
| `escalate-to-governance` | If the right fix is outside the envelope, the orchestrator may *ask* a risk owner to change the policy. Asking is all it can do (see layer 2). |

### The "never" list
| Rule | In plain words |
|---|---|
| `power-range` | Transmit power is never outside 30-46 dBm. |
| `tilt-range` | Tilt is never outside 4-10 degrees. |
| `critical-no-power-cut` | **Critical cells** (a hospital, an emergency dispatch centre, military) never lose power. |
| `critical-no-tilt-up` | ...and are never tilted away. |
| `critical-power-step-4db` | A critical cell may gain power, but in smaller steps: at most +4 dB. |
| `critical-neighbour-no-harm` | Never make a change the checks predict will hurt a critical cell somewhere else. |
| `stabilization-15m` | Never change the same cell twice within 15 minutes: let the network settle. |
| `one-autonomous-change-per-run` | Never more than one autonomous change per incident: a limit on the blast radius. An undo is not a change and stays allowed. |

---

## Layer 2: who may change the rules (`governance.cedar`)

The swarm's rules are themselves a file. Changing them is the one place a person must say yes.

| Rule | In plain words |
|---|---|
| `governance-change-needs-risk-owner-and-evidence` | **The only way in.** A risk owner approves *that exact* candidate, and its checks passed. |
| `agents-never-change-governance` | An agent never changes a policy or a guardrail, its own envelope least of all. |
| `only-risk-owner-approves-governance` | Separation of duties: an AI engineer or a network operator may not approve a change to the rules. |
| `no-change-with-failed-checks` | A candidate that does not validate, or drops a safety rule, is never activated, whoever approves it. |
| `approval-bound-to-candidate-hash` | The approval covers one file, identified by its SHA-256. Edit the file after the person looked and the approval is void. |
| `governance-is-not-self-amending` | These governance rules cannot be changed through this flow. Only code review in Git changes them. |

## Layer 3: who may change the agents (`promotion.cedar`)

A new prompt for an agent is a change to the agent, not to the network. It has its own gate.

| Rule | In plain words |
|---|---|
| `promote-needs-ai-engineer-and-evidence` | A change goes live only when an AI engineer approves and the evidence holds (target met, no open issue). |
| `agents-never-promote` | An agent never promotes anything, itself included. |
| `operators-cannot-approve-agent-changes` | Network operators approve network changes, not changes to the agents. |
| `no-promotion-below-target` | The target is declared before the experiment. Miss it and nothing goes live. |
| `no-promotion-with-open-signal-issue` | While Arize Signal has an open critical or high issue, nothing new goes live. |
| `loop-never-touches-policy` | Self-improvement can change how the swarm thinks, never what it is allowed to do. |

---

## The story to tell, in one minute

1. **The agents can fix a failing cell by themselves** (`apply-autonomous-within-envelope`), within hard numbers.
2. **They cannot hurt what matters most:** hospitals and emergency cells have their own, stricter rules.
3. **They cannot widen their own limits.** Only a named risk owner can, for one exact file, and the check that the file is unchanged is itself a rule.
4. **Every decision is recorded** with the policy that made it, and the same trace id runs from the Dynatrace problem to the command sent to the network.
5. **A pause is one change in a registry:** withdraw the model, and the next call is refused.
