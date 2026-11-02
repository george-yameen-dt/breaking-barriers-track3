# Submit Track 3: the no-brainer list

The deadline is **12:00, Nov 3 (Seattle time).** After that the account is read-only, and anything that calls a model stops working.

## 1. Before you record (about 15 minutes)

1. Do the incident run yourself on AgentCore and type `yes` when it asks. It breaks tower C on purpose; the five agents
   find it, a bad change is denied, you approve the fix and Cedar allows it:
   ```
   ./swarm.sh run --cloud --declare --fault outage-C
   ```
   Note the run id and commit it prints; every check it prints should say PASS.
2. The pause: Capacity answers once, its model is withdrawn, its next call is refused and the run goes on without it:
   ```
   ./swarm.sh run --cloud --declare --fault outage-C --pause
   ```
3. Put both run ids in `judging.json` (`run_ids` and `reported_results`) and copy it to S3 `judging/`.
4. Reprint every result, with no AWS needed:
   ```
   python3 tools/rebuild.py
   ```
5. Put the code in a repo (needs your go). Commit, push, and share the link with the judging contact.

## 2. The 5-minute video (record before 12:00 Nov 3)

Keep the commit hash and a run id on screen the whole time. `./swarm.sh run --cloud --declare` prints both.

| Time | Show | Say |
|---|---|---|
| 0:00–0:30 | The fault file `scenarios/faults/outage-C.json` | "We break the network on purpose: tower C stops transmitting and phones lose service." |
| 0:30–1:30 | The terminal running `./swarm.sh run --cloud --declare --fault outage-C`, and the AgentCore console with the 5 `track3_*` runtimes | "Five agents, each in its own AgentCore runtime. The orchestrator hands the same snapshot to three specialists at once; none sees the others' answers. Every model call, hand-off and tool call goes through one checkpoint." |
| 1:30–2:15 | The risk lines, the deny line, then your `yes` | "Code checks every idea and the risk critic reviews it. Tower B at 50 dBm: denied by policy. Switch C back on: allowed only after I approved that exact proposal, by its hash." |
| 2:15–2:45 | The stabilization line | "A second change to the same tower right away is denied: the network needs time to settle." |
| 2:45–3:30 | `tools/show_record.py --run-id … --trace` | "One trace id, every step across five runtimes, read straight from CloudWatch. That's the audit trail." |
| 3:30–4:15 | `./swarm.sh run --cloud --declare --fault outage-C --pause` | "Withdraw a model and its calls are refused; the swarm carries on with the other specialists." |
| 4:15–5:00 | The judge line and the PASS lines, then `tools/rebuild.py` | "A different AI family grades the agents. Every result rebuilds from our evidence." |

Not built yet: applying the approved change to the network (through the AgentCore Gateway) and checking it worked. Say so if asked; the run stops at "ready to apply".

## 3. The pitch (2:00 + 0:30)

> **Problem (20 s).** Operators won't let AI agents touch a live network. They need rules, a person in the loop, and proof of every step.
>
> **What we built (40 s).** Five agents (three specialists, a risk critic and an orchestrator, each in its own AgentCore runtime) fix a cell network, but every model call, hand-off and action crosses one checkpoint. A Cedar policy decides, Bedrock Guardrails screen what goes in and out, a person approves any change, and every step is written to CloudWatch and DynamoDB under one trace id.
>
> **Proof (40 s).** Live: "restart all towers" is denied, the rollback is allowed after approval, a withdrawn model is refused, and the trace rebuilds end to end. An independent judge from a different model family grades the agents. We also compare five agent models under one configuration, and a planted prompt injection is stopped.
>
> **Why it matters (20 s).** The controls live outside the agents they control. Swap any model and the rules, the audit trail and the person stay. Dynatrace gives operators the live view on top.

## 4. How we meet each judging criterion

| Criterion | Weight | Where it shows |
|---|---|---|
| Technical innovation | 25% | The controls sit outside the agents (`checkpoint.py`). Five models are compared under one configuration (`./track3.sh compare`). |
| Working demonstration | 20% | The live test is `tools/show_record.py`, which uses read-only calls only: S3, Logs Insights and DynamoDB. |
| Relevance to barriers | 20% | The barrier is "no agent touches the network without policy, approval and audit". Targets are declared before each run. |
| Production readiness | 10% | Agents and checkpoint run on AgentCore Runtime `track3_agents`; tools sit behind AgentCore Gateway `track3-gw`, where Policy in AgentCore (Cedar, ENFORCE) is a second wall. Records live in AWS under one run id, with AWS timestamps. |
| Collaboration and openness | 5% | Plain Python with reusable `checkpoint.py` and Cedar policies. GAPS.md lists every failure. |
| Presentation | 5% | This script: 2:00 + 0:30. |
| SDLC, trusted data, evidence chain | 15% | Frozen test set (hash declared), DATA.md, an independent judge, and an adversarial run. |

## 5. Ask the facilitators (Vara, Track 3 tech support)

- The judging deck says to run "the kit's check script". It isn't on the guide site. Where is it?
- Is the AgentCore runtime required for full marks on production readiness, or is a laptop agent with AWS records acceptable?
