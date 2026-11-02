Primary track: Track 3 (Multi-Agent Swarms for Application Enablement through NLMs)

# Agents that fix a cell network only through a checkpoint

**The problem, in three lines.**
- An AI change to a mobile network (radio model v2) cuts phones off.
- Operators want AI agents to find and fix that fast.
- They won't let an agent touch the network unless every step is decided by policy, screened, approved by a person, and recorded.

**What it does.**
1. A small simulated network runs on the laptop: 7 towers and 15 moving phones, using the open-source mobile-env simulator.
2. Dynatrace watches it. When model v2 cuts phones off, OpenPipeline raises a problem.
3. Two agents on Amazon Bedrock work the incident:
   - The **network agent** (openai.gpt-oss-120b) diagnoses it.
   - It hands off to the **change agent** (gpt-oss), which proposes fixes.
4. Every model call, every hand-off and every tool call crosses **one checkpoint** (`checkpoint.py`):
   - a **Cedar** policy decision (`policies/track3.cedar`)
   - **Bedrock Guardrails** on model input and output
   - an **audit record** with one run id and one trace id, written to CloudWatch Logs and DynamoDB
   - **a person's approval** before any change of state
5. The checkpoint **denies** "restart all towers" and **allows** "roll back to v1" only after a person approves it.
6. An **independent judge** grades the agents' answers: Claude, a different model family from the agents.
7. **Pause:** withdraw the agent model in the registry and its calls are refused, with a decision record.

**Test design.** The change agent is asked for a fast network-wide fix first and a safe fix second. That way the "never restart every tower" policy is exercised on every run, not left to chance.

**Architecture** (since 2026-11-01 19:18 PT; earlier runs ran the agents on the laptop).

```
Laptop simulator ─► Dynatrace (OpenPipeline → problem, Smartscape topology)  ← operator view
        ▲                                   │ facts
        │ rollback order                    ▼
        │                  ./track3.sh run  ──►  AgentCore Runtime track3_agents
        │                  (operator types yes)   network agent ─► handoff ─► change agent  (Bedrock gpt-oss)
        │                          │                       │
        │               DynamoDB approvals          checkpoint.py   Cedar · Bedrock Guardrails · approval · audit
        │                                                  │ tool calls
        │                                                  ▼
        └── DynamoDB control ◄── Lambda team04-track3-tools ◄── AgentCore Gateway track3-gw
                                                               Policy in AgentCore, Cedar, ENFORCE (second wall)
     CloudWatch Logs /bb26/team04/track3 · DynamoDB bb26-team04-track3-records · S3 evidence/   ← official record
     Dynatrace logs (track3-checkpoint) · Arize (bb26-hackathon)   ← every record, same trace id
```

AWS setup files are in `aws/` (IAM policies, Cedar rules, the tools Lambda); the runtime package is built by `agentcore/build.sh`.

**One command per stage** (in `track3/`; GlobalProtect up; `~/fyuz.env` fresh):

| Stage | Command |
|---|---|
| **Five agents on AgentCore** (one runtime each): break tower C, 3 specialists, risk critic, recommendation, deny, you approve, allow, judge | `./swarm.sh run --cloud --declare --fault outage-C` (asks you to type `yes`) |
| **Five agents: pause** (capacity's model withdrawn mid-run, the run goes on) | `./swarm.sh run --cloud --declare --fault outage-C --pause` |
| Five agents, dev run (not declared; add `--approve-as test-unattended` for no person) | `./swarm.sh run --cloud --fault outage-C` |
| *Below: the first, two-agent version (Nov 1), kept for its evidence* | |
| Simulator (the network) | `cd ../practice && ./demo.sh --story` |
| Register the models | `./track3.sh setup` |
| Incident: deny, approve, allow, roll back, judge | `./track3.sh run` (asks you to type `yes`) |
| Pause: withdraw a model, calls refused | `./track3.sh pause` |
| Freeze one real outage as the shared test set (once) | `./track3.sh freeze` |
| Compare agent models under one configuration | `./track3.sh compare` |
| Adversarial: prompt injection in the network data | `./track3.sh adversarial` |
| Arize Signal gate: an open critical/high Signal issue withdraws the agent model | `./track3.sh signal [--watch 60] [--dry-run]` |
| Register an agent change (a prompt version) as a candidate | `./track3.sh register-prompt --agent network-agent --version v2` |
| Promote it: only an AI engineer's `yes` with passing evidence (policies/promotion.cedar) | `./track3.sh promote --agent network-agent --version v2 --role ai_engineer --experiment-run <run_id>` |
| Live deny: an operator's approval, or the agent promoting itself | `./track3.sh promote ... --role network_operator` · `./track3.sh promote ... --role ai_engineer --as-agent` |
| Read one run back (read-only live test) | `python3 tools/show_record.py --run-id <run_id> [--item N] [--trace]` |
| Rebuild every result from evidence/, no AWS | `python3 tools/rebuild.py` |
| Offline tests (no AWS, Arize or Dynatrace) | `uv run --python 3.12 --with-requirements tests/requirements.txt pytest -q tests` |

**In progress (branch `governed-sdlc-loop`).** The improvement loop: a Dynatrace-measured outcome score (`outcome.py`, intents for voice and video), and agent changes promoted through the same checkpoint as network changes (`promotion.py`, `policies/promotion.cedar`). Design and target architecture: `../docs/superpowers/specs/2026-11-02-governed-sdlc-loop-design.md`, `../docs/architecture/target-architecture.pdf`.

**Before Nov 1.** The cell simulator, the Dynatrace pipeline and the Dynatrace app were built as practice before the event (`../practice`, `../cell-network-app`). The Track 3 checkpoint, policies, agents, records and evidence in this folder were written on Nov 1–3.
