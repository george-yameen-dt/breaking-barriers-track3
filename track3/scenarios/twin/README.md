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
| `adapters/twin_link.py` | One SSM Run Command per twin operation on the EC2 (the RSG answers that host only). Swap this for the proxy |
| `adapters/twin_dt.py` | The twin's per-window tower/network records and the agents' changes as Dynatrace logs (`service.name viavi-twin`), with a 30 s heartbeat |
| `scenarios/twin/healthy.py` | Builds RSG scenarios from the organisers' `maveric_4cell.json`: `team04-track3-4cell-healthy` (phone targets 2+1 Mb/s, QoS 100) and `-powercut` (+ S3 power failure at 180 s) |
| `scenarios/twin/healthy-power-cut.json`, `s3-power-failure.json` | Incident specs for `--twin-scenario` |
| `scenarios/twin/dynatrace/` | OpenPipeline pipeline `gy-viavi-twin` (Smartscape network + 4 towers + mesh, 8 `log.viavi_twin.*` metrics, tower-down Davis event), `smartscape.sh apply/undo/status`, dashboard builder |
| `tests/test_twin.py` | 16 offline tests |

## Facts learned (for the closed-loop build)
- With the sample's targets (8+10 Mb/s) the cells are full even when healthy: UE QoS Score about 34. The healthy copy
  uses 2+1 Mb/s and scores 100 on every tower.
- `RRU.PrbTotDl` is the load in %; `RRU.PrbUsedDl` is a PRB count (50 = full on 10 MHz LTE).
- Read `first_timestamp` after the first advance. An offline 180 s run takes about 15 s.
- SSM stdout is capped at 24k chars.
- Log records tagged with `dt.smartscape_source.id` show up in the problem card's logs.
- Dynatrace (yqs73977) live now: pipeline `gy-viavi-twin`, dashboard `c36c542f-96f1-4115-b6d4-d525f27d975c`.
