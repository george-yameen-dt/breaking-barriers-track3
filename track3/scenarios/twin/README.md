# The swarm on the Viavi twin (laptop path, 2 Nov 2026)

Tonight's working end-to-end loop: the five agents ran on a laptop, and the twin was reached through the team EC2 over
SSM. It is not on AgentCore yet; the closed-loop plan (nginx proxy and VPC-mode runtimes) replaces the transport.

```
VIAVI_RSG_URL=https://<rsg>/mcp ./swarm.sh run --twin live --twin-scenario healthy-power-cut --dt send
```

About 2 min. The twin runs healthy for 3 min, then S3 loses power at 180 s and Dynatrace opens a problem on Tower S3.
The agents propose a change, Cedar allows it after approval, it is applied on the twin and measured one window later,
and it is undone if things got worse. Measured: S4 40→44 dBm, phones without service 15→11.

## Files
| File | What it does |
|---|---|
| `adapters/twin.py` | `TwinNetwork`: ReplayNetwork's interface on the twin, plus `apply()` and `measure()`. Sources: `RecordedTwin` (rsg-14, offline) and `LiveTwin` |
| `adapters/twin_link.py` | One SSM Run Command per twin operation on the EC2 (the RSG answers that host only), or `LocalLink` in-process on the EC2 itself |
| `adapters/twin_dt.py` | The twin's per-window tower/network records and the agents' changes as Dynatrace logs (`service.name viavi-twin`), with a 30 s heartbeat (off in `run8.py`) |
| `scenarios/twin/healthy.py` | Builds RSG scenarios from the organisers' `maveric_4cell.json`: `team04-track3-4cell-healthy` (phone targets 2+1 Mb/s, QoS 100) and `-powercut` (+ S3 power failure at 180 s) |
| `scenarios/twin/healthy-power-cut.json`, `s3-power-failure.json` | Incident specs for `--twin-scenario` |
| `scenarios/twin/reset.py`, `baseline-4cell-powercut.json` | Puts `team04-track3-4cell-powercut` and `-live` back to the demo baseline (`viavi-reset` on the EC2) |
| `scenarios/twin/dynatrace/` | OpenPipeline pipeline `gy-viavi-twin` (Smartscape network + 4 towers + mesh, 8 `log.viavi_twin.*` metrics, tower-down Davis event), `smartscape.sh apply/undo/status`, dashboard builder |
| `tests/test_twin.py` | 23 offline tests |

## Facts learned (for the closed-loop build)
- With the sample's targets (8+10 Mb/s) the cells are full even when healthy: UE QoS Score about 34. The healthy copy
  uses 2+1 Mb/s and scores 100 on every tower.
- `RRU.PrbTotDl` is the load in %; `RRU.PrbUsedDl` is a PRB count (50 = full on 10 MHz LTE).
- Read `first_timestamp` after the first advance. An offline 180 s run takes about 15 s.
- SSM stdout is capped at 24k chars.
- Log records tagged with `dt.smartscape_source.id` show up in the problem card's logs.
- Dynatrace (yqs73977) live now: pipeline `gy-viavi-twin`, dashboard `c36c542f-96f1-4115-b6d4-d525f27d975c`.

## The 8-minute test on the EC2 (operator, 3 Nov 2026)
`run8.py` also runs on the team EC2 itself and calls the RSG directly (`--link local`, no SSM). The operator starts it,
never Dynatrace or the agents:

```
ssh hackathon-ec2 ~/team04-track3/viavi-run                     # team04-track3-4cell-powercut, config "baseline"
ssh hackathon-ec2 ~/team04-track3/viavi-run --scenario team04-track3-live --config-version <v>
```

Teammates without the ssh alias: `aws ssm start-session --target i-06e27097bc0c6bef8 --region us-east-2`, then
`sudo -iu ec2-user ~/team04-track3/viavi-run`. Add `--dt dry-run` to send nothing (records go to `dev-runs/<run_id>/`).

What it does: checks the run slot (`simulation_status current`) and refuses if another run is going (never `force`, never
stops it); starts the scenario offline and paced; each wall-clock minute advances 60 s and sends one network record
and one per tower (`service.name viavi-twin`, no heartbeat), plus new twin events; stops the run at minute 8, also on
error or Ctrl-C. Every record carries `trace_id`, `twin.run_id`, `twin.test_id` and `twin.config_version`.

### Reset to the demo baseline
```
ssh hackathon-ec2 ~/team04-track3/viavi-reset            # rewrite -powercut and -live from baseline-4cell-powercut.json
ssh hackathon-ec2 ~/team04-track3/viavi-reset --check    # compare only (exit 1 if a scenario has drifted)
ssh hackathon-ec2 ~/team04-track3/viavi-reset --verify   # also a fast 4-min run: >= 70 % coverage, then < 70 % from minute 4
```

The baseline: S1 to S4 at 40 dBm and tilt 6, and S3 loses commercial power at 180 s, with no other anomaly (about
77 % coverage, then about 65 %). The agents' `tx_power`/`antenna_tilt` commands change only the run they are sent to,
so a fresh `viavi-run` starts at 40 dBm anyway. The reset undoes everything else: edits in the UI, test saves, an
overwritten scenario. It writes nothing while another run is going. `--verify` sends nothing to Dynatrace.

| On the EC2 | What |
|---|---|
| `~/.config/team04/dt.env` (mode 600, operator-made) | `DT_PLATFORM_TOKEN=` (platform token, scope `storage:logs:write`) and `VIAVI_RSG_URL=https://<rsg>/mcp` |
| `~/team04-track3/` | `track3/` (git-tracked files), `viavi-run`, `viavi-reset`, `.venv` (Python 3.11 + boto3), `REVISION` (the deployed commit) |
| `~/team04-track3/track3/dev-runs/<run_id>/` | `twin-8min.jsonl`: the windows and events of each run |

Deploy from a laptop checkout, after committing: `track3/scenarios/twin/deploy-ec2.sh` (add `--setup` the first time:
installs python3.11 with dnf and the venv). It copies the tracked files of `track3/` over rsync and never deletes.
