// DQL behind every view. All data is the mobile-env simulator, one log record per step (service.name "mobile-env").

const sim = (from: string) => `fetch logs, from: now()-${from}
| filter service.name == "mobile-env"`;

const frameFields = `fields timestamp,
    policy = mobile.policy, policyName = mobile.policy_name,
    episode = toLong(mobile.episode), step = toLong(mobile.step),
    active = toLong(mobile.phones_active), served = toLong(mobile.phones_served),
    dropped = toLong(mobile.phones_dropped), rate = toDouble(mobile.median_rate),
    ues = mobile.ues, towers = mobile.towers, w = toDouble(mobile.map_w), h = toDouble(mobile.map_h),
    story = mobile.story_phase`;

/**
 * Steps newer than `cursor` (ISO time), oldest first. Grail makes logs queryable about 15-20 s after ingest,
 * so the app asks only for what it hasn't seen and replays it at the simulator's pace.
 */
export const framesSince = (cursor: string) => `fetch logs, from: "${cursor}"
| filter service.name == "mobile-env"
| filter isNotNull(mobile.towers) and timestamp > toTimestamp("${cursor}")
| sort timestamp asc
| limit 600
| ${frameFields}`;

/** The newest step, from up to a day back, so the map still shows something when the simulator is off. */
export const lastFrame = `${sim("24h")}
| filter isNotNull(mobile.towers)
| sort timestamp desc
| limit 1
| ${frameFields}`;

/** v1 vs v2 over the last 15 minutes. */
export const comparison = `${sim("15m")}
| summarize steps = count(),
    dropped = avg(toDouble(mobile.phones_dropped)),
    servedPct = avg(toDouble(mobile.phones_served) / toDouble(mobile.phones_active)) * 100,
    rate = avg(toDouble(mobile.median_rate)),
    by: {policy = mobile.policy}
| sort policy asc`;

/** Phones dropped per 10 s over the last 10 minutes, with the model that was running. */
export const timeline = `${sim("10m")}
| fieldsAdd bucket = bin(timestamp, 10s)
| summarize dropped = max(toDouble(mobile.phones_dropped)), policy = takeLast(mobile.policy), by: {bucket}
| sort bucket asc`;

/** Raw steps, for "See the data". */
export const rawSteps = `${sim("15m")}
| sort timestamp desc
| fields timestamp, loglevel, content, mobile.policy, mobile.phones_served, mobile.phones_dropped, mobile.median_rate`;

const SAFETY_EVENT = "Cell network safety rule broken";

/** The latest problem Dynatrace Intelligence opened from the OpenPipeline safety event. */
export const cellProblem = `fetch dt.davis.problems, from: now()-2h
| filter event.name == "${SAFETY_EVENT}"
| sort event.status asc, event.start desc
| limit 1`;

/** The topology OpenPipeline builds in Smartscape from the simulator's logs. */
export const topologyNodes = `smartscapeNodes "CUSTOM_*"
| filter in(type, {"CUSTOM_CELL_NETWORK", "CUSTOM_CELL_TOWER", "CUSTOM_MOBILE_PHONE"})
| fields id, name, type`;

export const topologyLinks = `smartscapeEdges "connects_to"
| filter source_type == "CUSTOM_MOBILE_PHONE" and target_type == "CUSTOM_CELL_TOWER"
| summarize links = count()`;

/** Last v1 step before the problem started: the moment v2 went live is right after it. */
export const lastV1Before = (startIso: string) => `fetch logs, from: toTimestamp("${startIso}") - 30m, to: toTimestamp("${startIso}")
| filter service.name == "mobile-env" and mobile.kind == "step" and mobile.policy == "v1"
| summarize lastV1 = max(timestamp)`;

/** When v2's first step ran and when the first phone was cut off, after `fromIso`. */
export const v2Timeline = (fromIso: string, startIso: string) => `fetch logs, from: toTimestamp("${fromIso}"), to: toTimestamp("${startIso}") + 2m
| filter service.name == "mobile-env" and timestamp > toTimestamp("${fromIso}")
| summarize v2On = min(if(mobile.kind == "step" and mobile.policy == "v2", timestamp)),
    firstDrop = min(if(mobile.kind == "safety", timestamp))`;

/** The ops agent's latest advice (agent.py writes it back as a log after asking Claude). */
export const agentAdvice = `fetch logs, from: now()-2h
| filter service.name == "cell-ops-agent" and (isNull(agent.state) or in(agent.state, {"acted", "advised"}))
| sort timestamp desc
| limit 1
| fields timestamp, content, agent.problem, agent.root_cause, agent.evidence, agent.action, agent.acted,
    agent.confidence, agent.model, agent.input_tokens, agent.output_tokens, agent.cost_usd, agent.duration_ms`;

/** The agent's latest heartbeat: watching, thinking, acted or advised. */
export const agentStatus = `fetch logs, from: now()-30m
| filter service.name == "cell-ops-agent"
| sort timestamp desc
| limit 1
| fields timestamp, content, agent.state, agent.problem`;

/** App experience per model: share of phone-steps where each app would work, from each phone's data rate. */
export const experience = `${sim("15m")}
| filter mobile.kind == "step"
| parse mobile.ues, "JSON_ARRAY:ues"
| expand ues
| fieldsAdd rate = toDouble(ues[rate])
| summarize phoneSteps = count(),
    call = 100.0 * countIf(rate >= 0.1) / count(),
    video = 100.0 * countIf(rate >= 1.5) / count(),
    reels = 100.0 * countIf(rate >= 3) / count(),
    feed = 100.0 * countIf(rate >= 8) / count(),
    noSignal = 100.0 * countIf(rate <= 0) / count(),
    by: {policy = mobile.policy}`;

/** Every answer the ops agent gave in the last 2 hours. */
export const agentRuns = `fetch logs, from: now()-2h
| filter service.name == "cell-ops-agent" and (isNull(agent.state) or in(agent.state, {"acted", "advised"}))
| sort timestamp desc
| limit 20
| fields timestamp, content, agent.problem, agent.action, agent.acted, agent.model,
    agent.input_tokens, agent.output_tokens, agent.cost_usd, agent.duration_ms`;

/**
 * Every record the Track 3 checkpoint wrote in the last 6 hours. track3/checkpoint.py sends a copy of each one
 * (decision, guardrail, model call, approval, ...) as a log; `content` is "<kind>: <the record as JSON>".
 */
export const checkpointRecords = `fetch logs, from: now()-6h
| filter service.name == "track3-checkpoint"
| sort timestamp desc
| limit 1000
| fields timestamp, content, trace_id, run = track3.run_id, seq = toLong(track3.seq), kind = track3.kind,
    decision = track3.decision, action = track3.action`;

/** One run's records in order, for "See the data". */
export const runRecords = (run: string) => `fetch logs, from: now()-6h
| filter service.name == "track3-checkpoint" and track3.run_id == "${run}"
| sort toLong(track3.seq) asc
| fields timestamp, track3.seq, track3.kind, track3.decision, track3.action, trace_id, content`;

/** The problem a run's network agent read, by its P- number. */
export const problemByDisplayId = (displayId: string) => `fetch dt.davis.problems, from: now()-24h
| filter display_id == "${displayId}"
| sort timestamp desc
| limit 1`;

/** Worst 10 s of dropped phones over 2 hours, to check whether each rollback really fixed things. */
export const dropsHistory = `${sim("2h")}
| filter mobile.kind == "step"
| fieldsAdd bucket = bin(timestamp, 10s)
| summarize dropped = max(toDouble(mobile.phones_dropped)), by: {bucket}
| sort bucket asc`;
