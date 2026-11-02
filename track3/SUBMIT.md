# Submit Track 3: the no-brainer list

The deadline is **12:00, Nov 3 (Seattle time).** After that the account is read-only, and anything that calls a model stops working.

## 1. Before you record (about 15 minutes)

1. Start the network in its own terminal:
   ```
   cd ../practice && ./demo.sh --story
   ```
2. Do the incident run yourself and type `yes` when it asks:
   ```
   ./track3.sh run
   ```
   Note the run id it prints.
3. Withdraw a model and watch its calls get refused:
   ```
   ./track3.sh pause
   ```
4. Reprint every result, with no AWS needed:
   ```
   python3 tools/rebuild.py
   ```
5. Put the code in a repo (needs your go). Commit, push, and share the link with the judging contact.

## 2. The 5-minute video (record before 12:00 Nov 3)

Keep the commit hash and a run id on screen the whole time. `./track3.sh run` prints both.

| Time | Show | Say |
|---|---|---|
| 0:00–0:30 | The simulator window and the Dynatrace app | "A new radio model just went live and is cutting phones off. Dynatrace caught it." |
| 0:30–1:30 | The terminal running `./track3.sh run` | "Two AI agents work the problem: one diagnoses it, one proposes fixes. Every call goes through one checkpoint." |
| 1:30–2:15 | The deny line, then your `yes` | "Restart all towers: denied by policy. Roll back: allowed only because I approved it." |
| 2:15–2:45 | The sim window, phones going green | "The rollback ran and the phones are back." |
| 2:45–3:30 | `tools/show_record.py --run-id … --trace` | "One trace id, every step, read straight from CloudWatch. That's the audit trail." |
| 3:30–4:15 | `./track3.sh pause` | "Withdraw a model and its calls are refused, with a decision record." |
| 4:15–5:00 | The judge line, and `tools/rebuild.py` | "A different AI family grades the agents. Every result rebuilds from our evidence." |

## 3. The pitch (2:00 + 0:30)

> **Problem (20 s).** Operators won't let AI agents touch a live network. They need rules, a person in the loop, and proof of every step.
>
> **What we built (40 s).** Two agents fix a cell network, but every model call, hand-off and action crosses one checkpoint. A Cedar policy decides, Bedrock Guardrails screen what goes in and out, a person approves any change, and every step is written to CloudWatch and DynamoDB under one trace id.
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
