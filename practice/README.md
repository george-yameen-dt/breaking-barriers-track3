# Cell network demo (mobile-env → yqs73977)

Real simulator, real data. 7 towers, 15 moving phones. A model picks which tower each phone uses.

- **v1 safe:** every phone gets its best tower.
- **v2 cheater:** strong phones grab every tower they can reach; weak phones get nothing.
- **Result:** v2 makes served phones about 2.8x faster (median rate 1.2 → 3.5) but drops about 5 of 15 phones every step. v1 drops 0.

## Run it

GlobalProtect has to be up.

**Easiest: one command, hands-free.** It plays the 3-step story on a loop.

```bash
./run-demo.sh
```

1. **① Healthy:** model v1 runs for 45 s and every phone has a tower.
2. **② Broken:** model v2 goes live and cuts phones off. Dynatrace opens a problem about 1 min later.
3. **③ AI fixes it:** the agent asks Claude, which explains the problem, and the agent rolls back to v1. The next loop starts 3 min later.

The agent's output goes to `agent.log`. Close the sim window or press Ctrl-C to stop both.

**Manual control:**

```bash
./demo.sh            # window opens on v1; type 2 + Enter for v2, 1 for v1, q to quit
./demo.sh --auto 60  # hands-free, flips v1/v2 every 60 s
./demo.sh --headless --episodes 2   # no window, just send data
```

- **App (the demo screen):** https://yqs73977.sprint.apps.dynatracelabs.com/ui/apps/my.cell.network.live
- **Older dashboard:** https://yqs73977.sprint.apps.dynatracelabs.com/ui/apps/dynatrace.dashboards/dashboard/e4c63a74-6866-40ac-87d0-c88ba6d4afa7

## Timing to know

- Grail makes logs searchable about 18 s after they arrive, so the app replays about 20 s behind the laptop.
- After you switch to v2, the problem opens in about 1–2 minutes.
- After you switch back to v1, the problem closes about 2 minutes after the last violation, plus the same delay.
- Start v2 a couple of minutes before you need the problem card on screen.

## The 2-minute show-off

1. Open the app. Start the sim on v1. The strip across the top shows the whole path: Simulator → OpenPipeline → Smartscape → Dynatrace Intelligence → AppEngine.
2. "A model decides which tower each phone connects to. Every step goes to Dynatrace as a log."
3. "OpenPipeline turns those logs into a live Smartscape topology: the network, 7 towers, 15 phones, and which phone talks to which tower." Click a tower; it opens in Smartscape.
4. Type `2`. "v2 is the new candidate. Served phones get about 3x faster. Looks great."
5. About 20 s later phones turn red. The safety rule card says "Broken · 5 phones cut off", and towers sit idle next to phones with no signal.
6. 1–2 minutes later the red banner shows a problem (P-…) raised by OpenPipeline. Click **See it in Smartscape** to open the problem graph on the cell network.
7. "v2 learned to drop the hard-to-serve phones. Dynatrace caught it from the data alone, before anyone shipped it. That's the evidence a risk owner needs."

## How it works

- **Simulator:** `sim.py` runs mobile-env (`mobile-medium-central-v0`) and posts to the logs ingest API on the platform domain. It uses `DT_PLATFORM_TOKEN` from `~/.bindplane/.env`, which is kept off OneDrive.
- **Records it sends** (field `mobile.kind` tells them apart):
  - `step`: one per step, carrying the full frame the app replays.
  - Once a second: `tower` (one per tower), `phone` (one per phone-to-tower link), and `safety` (only when phones are dropped).
- **OpenPipeline:** the pipeline "Cell network (mobile-env)" (customId `gy-cell-network`) is in `openpipeline/pipeline.json`. A routing entry sends `service.name == "mobile-env"` to it. It does four things:
  - **Smartscape nodes:** `CUSTOM_CELL_NETWORK`, `CUSTOM_CELL_TOWER` and `CUSTOM_MOBILE_PHONE`, each with a `belongs_to` edge to the network.
  - **Smartscape edges:** `CUSTOM_MOBILE_PHONE` → `connects_to` → `CUSTOM_CELL_TOWER`.
  - **Metrics:** `log.cellnet.phones_dropped`, `log.cellnet.median_rate` and `log.cellnet.tower_load` (the last one is attached to tower nodes).
  - **Davis event** "Cell network safety rule broken" on `safety` records. It is attached to the network node and its timeout is 2 minutes.
- **App:** the source is in `../cell-network-app`; deploy it with `./deploy.sh`. It reads the steps, the problem, and the Smartscape nodes and edges. Clicking a tower or phone opens it in Smartscape.
- **Definition of dropped:** in range of a tower, but with no connection for 2 or more steps.
- **Python venv:** `~/.local/venvs/mobile-env` (Python 3.12, kept off OneDrive).

## AI ops agent (Claude + Arize + Dynatrace)

```bash
./agent.sh --act   # in a second terminal, next to ./demo.sh
```

- **What it does:**
  - Every 15 s it checks Dynatrace for an open "Cell network safety rule broken" problem.
  - When one opens, it reads the live network state from Grail and asks Claude what happened.
  - It writes Claude's answer back to Dynatrace as a log (`service.name == "cell-ops-agent"`). The app shows it as the "AI ops agent" card.
  - With `--act`, if Claude says roll back, it writes `control.json`. The sim reads that file and switches to v1, the phones go green, and the problem closes.
- **Model:** Claude via the `claude` CLI, headless, with no tools and no project context. It costs about $0.004 a call. At the hackathon you'd swap in Bedrock in the team account.
- **Traces:** each run is one trace with the steps read_network_state → chat claude → rollback_to_v1 → write_advice_to_dynatrace. The spans carry both GenAI and OpenInference attributes.
  - **Arize:** traces go to project `bb26-hackathon` in the team space (agentic-aeric), key in `~/.arize_api_key_bb26`. Arize Signal reviews that project every 6 hours and files issues.
  - **Dynatrace AI Observability:** set `DT_TRACES_TOKEN` in `~/.bindplane/.env` to a token with scope `openpipeline:traces:ingest`, and the same traces land there too.
- **Story:** Dynatrace finds the problem and its blast radius. The AI agent explains it and fixes it. Arize grades the agent, and Dynatrace watches it in production.

## Undo

- Delete the pipeline: `dtctl delete settings <pipeline objectId>`.
- Restore routing from `openpipeline/routing.before.json`. It's the shared routing object, so keep other SEs' entries.

## Next

- Add a Site Reliability Guardian with the objective "phones dropped = 0" to fail v2 automatically.
- Swap in the hackathon's own gym and twin when they hand them out.
