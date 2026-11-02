# Data

Synthetic data only. No operator logs, no customer data.

| Data | Source | Licence | How it is made |
|---|---|---|---|
| Cell network steps (phones, towers, links, data rates) | mobile-env simulator, scenario `mobile-medium-central-v0` (github.com/stefanbschneider/mobile-env) | MIT | `../practice/sim.py` (seeded per episode) |
| Radio models v1 and v2 | Rule-based policies in `../practice/sim.py` (`actions_for`) | ours | v1: best tower per phone. v2: strong phones take every tower, weak phones get none |
| Incident scenario (frozen test set) | `scenarios/incident-v2-cutoff.json` | ours | hand-written. Its SHA-256 is in every run's declaration |
| Policies | `policies/track3.cedar` | ours | hand-written. Its SHA-256 is in every run's declaration |
| Promotion policies | `policies/promotion.cedar` | ours | hand-written. Its SHA-256 is in every promotion run's declaration |
| Agent prompt versions | `prompts/<agent>/<version>.txt` | ours | `v1` is the running prompt (`incident.DIAGNOSE`, byte-identical, checked by a test). Each candidate's SHA-256 is in its registry item and its promotion declaration |
| Business intents | `intents/*.json` | ours | hand-written thresholds and weights for the outcome score (`coverage_first` for voice, `throughput_first` for video) |
| Dynatrace problem and network state | Dynatrace tenant yqs73977, from the simulator's logs | n/a | read at run time. Recorded in each run's `network_read` record |

## Model outputs we rely on

Every model call is recorded in `evidence/runs/<run_id>/records.jsonl`, in CloudWatch Logs and in DynamoDB. Each record (`kind: model_call`) carries:
- `model_id`
- `request_id` (the Bedrock request id)
- `prompt_sha256` (the system prompt plus the user prompt)
- `output_sha256`
- the first 2,000 characters of the output

The prompts live in `track3.py` (`DIAGNOSE`, `PROPOSE`, `JUDGE`).

| Role | Model | Family |
|---|---|---|
| Network agent, change agent | `openai.gpt-oss-120b-1:0` | OpenAI gpt-oss |
| Independent judge | `us.anthropic.claude-haiku-5-5` | Anthropic (different family from the agents) |
