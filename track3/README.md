Primary track: Track 3 (Multi-Agent Swarms for Application Enablement through NLMs)

# Agents that fix a cell network on their own, inside Cedar limits

**The problem, in three lines.**
- A fault in a mobile network (a tower loses power) leaves phones without service while operators are still reading alarms.
- Operators want AI agents to find the cause and fix it fast, without waiting for a person.
- They will not let an agent touch the network unless every step is decided by policy, screened, recorded and traceable end to end.

**What it does.**
1. **Viavi AI RAN** runs a 4-cell scenario (the organisers' `maveric_4cell` sample). A poller on the team EC2 (`scenarios/twin/run8.py`) sends one network record and one record per cell to Dynatrace every minute, with the cell's real Viavi name (`S4/B3/C1`), site, band, PCI and a wall-clock time.
2. **Dynatrace** (OpenPipeline `gy-viavi-twin-cells`) builds the Smartscape: network, towers (sites) and **cells**, with neighbour links, and extracts cell metrics (QoS, load, phones, phones without service, power, edge RSRP, cell enabled) with the cell and its criticality as dimensions.
3. **Davis anomaly detectors** on those metrics (not on log fields) raise events on the cell; an **event correlation rule** groups the events of one network into **one problem**. A workflow sends the problem to a Lambda, which declares the run's targets in S3 and starts the orchestrator on AgentCore.
4. **Five agents** on Amazon Bedrock, each in its own AgentCore runtime, work the problem: three specialists (coverage, capacity, availability) propose changes in their own domain, a risk critic assesses them, the orchestrator picks one. They do not read Dynatrace themselves: they get the problem (id, title, the cells and their criticality) from the Dynatrace workflow, and read the network through the Viavi MCP.
5. The orchestrator **applies the change through the Viavi MCP** (`simulation_command`: `tx_power` or `antenna_tilt`), **measures the next window**, and **undoes it if it made things worse**. No person approves a network change: Cedar's envelope decides (see below).
6. **Critical cells** (a hospital, an emergency dispatch centre; synthetic labels in `scenarios/twin/critical-cells.json`) are protected: Cedar forbids weakening them, caps their power step, and forbids a change predicted to hurt one; verification fails a change that lowers one's QoS.
7. Every model call, tool call and hand-off crosses **one checkpoint** (`checkpoint.py`): a **Cedar** decision (`policies/swarm.cedar`), **Bedrock Guardrails** on model input and output, and an **audit record** in CloudWatch Logs and DynamoDB with one run id and one trace id. **Every run is traced** to CloudWatch, Dynatrace and Arize (service `multi-agent-swarm`); a run without credentials for either destination is refused, never run untraced.
8. **Human approval is for the governance layer only**: changes to the agents themselves (`promotion.py`, `policies/promotion.cedar`) need a person's `yes`. This was agreed with a cohort lead and is declared in `GAPS.md`.
9. An **independent judge** (Claude, a different model family from the agents) grades the diagnosis and the fix. **Pause:** withdraw the agent model in the registry and its calls are refused, with a decision record.

**The envelope (what Cedar lets the agents do alone).** `tx_power` within 30-46 dBm, from 6 dB down to 8 dB up per change; tilt 4-10 degrees, at most 5 per change; one change per run; no second change to a cell within 15 minutes. Critical cells: no power cut, no tilt up, at most +4 dB. Tools: Viavi read calls, plus `tx_power` and `antenna_tilt` for the orchestrator, with the run's trace id in the command's reason. Everything else (`raw`, `turn_off`, handovers, starting or stopping a run, scenario writes) is denied.

**Test design.** Every run probes Cedar with a change that must be denied (`deny_probe`) and, after the fix, tries the same cell again right away (`stabilization_probe`). The checks (`tools/check.py`) read only the audit records: `swarm_autonomous_apply`, `swarm_verify`, `swarm_deny`, `swarm_stabilization`, `swarm_trace`, `critical_safe`, `problem_to_command`, `swarm_pause`.

**Architecture.**

```
Viavi AI RAN (4 cells) ──► poller on the team EC2 (run8.py) ──► Dynatrace
                                                                 OpenPipeline gy-viavi-twin-cells: Smartscape network > tower > cell, cell metrics
                                                                 Davis detectors on the metrics ─► correlation rule ─► ONE problem
                                                                                                          │ workflow
                                          Lambda track3-trigger: one run per incident, stable trace id, declares targets in S3
                                                                                                          ▼
            AgentCore (VPC): track3_orchestrator ─► coverage · capacity · availability · risk   (every agent on Strands / Bedrock)
                  every call ─► checkpoint.py: Cedar (swarm.cedar) · Bedrock Guardrails · audit record (one trace id)
                  reads: Viavi MCP (read allow-list)   writes: Viavi MCP http://10.0.1.59/mcp  tx_power / antenna_tilt only
                                                  then: measure the next window, undo if worse
   Official record: CloudWatch Logs /bb26/team04/track3 · DynamoDB bb26-team04-track3-records · S3 evidence/
   Operator views:  Dynatrace · Arize (bb26-hackathon), same trace id
```

AWS setup files are in `aws/`; the runtime package is built by `agentcore/build_swarm.sh`. Dynatrace configuration is in `scenarios/twin/dynatrace/` (`smartscape.sh`, `apply-detection.sh`) and `workflows/`.

**One command per stage** (in `track3/`; GlobalProtect up; `~/fyuz.env` fresh):

| Stage | Command |
|---|---|
| Register the models and tools (also restores a model the Signal gate withdrew) | `./track3.sh setup` |
| Pipeline: Smartscape cells and cell metrics (needs the Dynatrace tenant) | `scenarios/twin/dynatrace/smartscape.sh apply` |
| Detectors and the correlation rule | `scenarios/twin/dynatrace/apply-detection.sh apply` |
| Build and deploy the runtimes and the trigger | `agentcore/build_swarm.sh` · `aws/swarm_setup.py` · `aws/trigger_setup.py` · `workflows/apply.sh` |
| **The live run**: the operator starts the Viavi scenario; Dynatrace, the workflow and the swarm do the rest | on the EC2: `~/team04-track3/viavi-run --scenario team04-track3-live --minutes 16` |
| The swarm on a laptop, attached to the live run | `./swarm.sh run --twin attach` |
| The swarm on the saved Viavi run (offline) | `./swarm.sh run --twin recorded` |
| **Pause**: capacity's model withdrawn mid-run, calls refused, the run goes on | POST a problem with `"pause": true` to the trigger Lambda while a Viavi run is going (see `aws/trigger_lambda.py`), or `./swarm.sh run --twin recorded --pause` |
| Arize Signal gate: an open critical/high Signal issue withdraws the agent model | `./track3.sh signal [--watch 60] [--dry-run]` |
| Register an agent change (a prompt version) as a candidate | `./track3.sh register-prompt --agent network-agent --version v2` |
| Promote it: only an AI engineer's `yes` with passing evidence (the governance gate) | `./track3.sh promote --agent network-agent --version v2 --role ai_engineer --experiment-run <run_id>` |
| Read one run back (read-only live test) | `python3 tools/show_record.py --run-id <run_id> [--item N] [--trace]` |
| **Rebuild every result from evidence/, no AWS** | `python3 tools/rebuild.py` |
| Offline tests (no AWS, Arize or Dynatrace) | `uv run --python 3.12 --with-requirements tests/requirements.txt pytest -q tests` |

**Results.** See `judging.json` for the run ids and `evidence/results.json` for each unit's pass or fail. RESULTS_PLACEHOLDER

**Before Nov 1.** The mobile-env cell simulator, its first OpenPipeline pipeline and the Dynatrace app `../cell-network-app` were built as practice before the event; the simulator and its polling agent were removed from this repo (they are in git history). The checkpoint, the policies, the agents, the Viavi twin work, the Smartscape cells, the detectors, the trigger and the evidence chain were built during the event (see `git log`).

**Credit.** The Viavi poller (`scenarios/twin/run8.py`, `adapters/twin*.py`) and the first `gy-viavi-twin` pipeline were built by teammates; this work extends both (cell level, metrics, detectors, correlation, autonomous apply, tracing).

**Known limits** are in `GAPS.md`; the data and licences are in `DATA.md`.
