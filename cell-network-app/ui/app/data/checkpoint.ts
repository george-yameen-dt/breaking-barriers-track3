// The Track 3 checkpoint's audit records, read back from Grail and turned into runs, proofs and plain sentences.
import { num, str, type Row } from "./hooks";

/** One record the checkpoint wrote. `f` holds the record's own fields (who, tool, policies, ...). */
export interface CpRecord {
  t: number;
  run: string;
  traceId: string;
  seq: number;
  kind: string;
  decision: string;
  action: string;
  f: Record<string, unknown>;
}

export interface Run {
  id: string;
  /** incident, pause or setup, from the run id. */
  type: string;
  traceId: string;
  start: number;
  end: number;
  records: CpRecord[];
  complete: boolean;
  /** Why the trace is not complete, in words; empty when it is. */
  gap: string;
  /** The problem the network agent read (P-…), if any. */
  problem: string;
}

// `"key": value` at the top level of a record. Nested objects are taken whole, so their keys don't leak out.
const PAIR = /"([a-z_]+)": ("(?:[^"\\]|\\.)*"|\[[^\]]*\]|\{[^{}]*\}|-?\d+(?:\.\d+)?|null|true|false)/g;

/** The checkpoint cuts `content` at 900 characters, so long records (model calls) can't be parsed whole. */
function parseFields(content: string, kind: string): Record<string, unknown> {
  const body = content.startsWith(`${kind}: `) ? content.slice(kind.length + 2) : content;
  try {
    const whole: unknown = JSON.parse(body);
    if (whole && typeof whole === "object") return whole as Record<string, unknown>;
  } catch {
    // cut off: read what is there, field by field
  }
  const out: Record<string, unknown> = {};
  for (const m of body.matchAll(PAIR)) {
    if (m[1] in out) continue;
    try {
      out[m[1]] = JSON.parse(m[2]) as unknown;
    } catch {
      // a value cut off mid-way
    }
  }
  return out;
}

export function toRecords(rows: Row[]): CpRecord[] {
  return rows
    .map((r) => {
      const kind = str(r.kind);
      return {
        t: Date.parse(str(r.timestamp)) || 0,
        run: str(r.run),
        traceId: str(r.trace_id),
        seq: num(r.seq),
        kind,
        decision: str(r.decision),
        action: str(r.action),
        f: parseFields(str(r.content), kind),
      };
    })
    .filter((r) => r.run && r.kind);
}

function check(records: CpRecord[], now: number): { complete: boolean; gap: string } {
  for (let i = 0; i < records.length; i++) {
    if (records[i].seq !== i + 1) return { complete: false, gap: `record ${i + 1} is missing` };
  }
  if (new Set(records.map((r) => r.traceId)).size > 1) return { complete: false, gap: "more than one trace id" };
  if (records[records.length - 1]?.kind !== "run_end") {
    const last = records[records.length - 1]?.t ?? 0;
    return { complete: false, gap: now - last < 90_000 ? "still running" : "stopped before the end" };
  }
  return { complete: true, gap: "" };
}

/** Newest run first. */
export function toRuns(records: CpRecord[], now: number): Run[] {
  const byRun = new Map<string, CpRecord[]>();
  for (const r of records) byRun.set(r.run, [...(byRun.get(r.run) ?? []), r]);
  return [...byRun.entries()]
    .map(([id, rs]) => {
      rs.sort((a, b) => a.seq - b.seq);
      const read = rs.find((r) => r.kind === "network_read");
      return {
        id,
        type: /track3-([a-z]+)-\d+$/.exec(id)?.[1] ?? "run",
        traceId: rs[0].traceId,
        start: rs[0].t,
        end: rs[rs.length - 1].t,
        records: rs,
        ...check(rs, now),
        problem: str(read?.f.problem),
      };
    })
    .sort((a, b) => b.start - a.start);
}

// ---- words -------------------------------------------------------------------------------------

/** openai.gpt-oss-120b-1:0 -> gpt-oss-120b; us.anthropic.claude-haiku-5-5 -> Claude Haiku 5.5 */
export function modelName(id: string): string {
  const claude = /claude-([a-z]+)-(\d+)-(\d+)/.exec(id);
  if (claude) return `Claude ${claude[1][0].toUpperCase()}${claude[1].slice(1)} ${claude[2]}.${claude[3]}`;
  return id.replace(/^[a-z]+\./, "").replace(/-\d+:\d+$/, "");
}

/** Agent::"change-agent" or change-agent -> Change agent */
export function agentName(who: unknown): string {
  const s = str(who).replace(/^Agent::"?|"$/g, "");
  if (!s) return "Checkpoint";
  const w = s.replace(/-/g, " ");
  return w[0].toUpperCase() + w.slice(1);
}

const TOOLS: Record<string, string> = {
  read_network: "read the network",
  handoff: "hand the work to the change agent",
  restart_all_towers: "restart every tower",
  rollback_model: "roll the radio model back to v1",
  set_tower_power: "change one tower's power",
};

export function toolWords(tool: string, resource = ""): string {
  if (tool === "invoke_model") return `call ${modelName(resource.replace(/^Model::"?|"$/g, ""))}`;
  return TOOLS[tool] ?? tool.replace(/_/g, " ");
}

const policyList = (v: unknown): string[] => (Array.isArray(v) ? v.map((p) => String(p)) : []);

export type Tone = "ok" | "meh" | "bad";

export interface Step {
  actor: string;
  text: string;
  /** A second, quieter line (an approval's note, a judge's reason). */
  sub?: string;
  chip?: { label: string; tone: Tone };
  policies: string[];
  /** Shown in "Key steps"; routine allows, guardrail passes and model calls are not. */
  key: boolean;
}

/** One record as a sentence a room can read. */
export function describe(r: CpRecord): Step {
  const f = r.f;
  const who = agentName(f.who);
  switch (r.kind) {
    case "run_start":
      return { actor: "Checkpoint", text: str(f.purpose) ? `Run started: ${str(f.purpose)}` : `Incident run started · ${str(f.scenario)}`, policies: [], key: true };
    case "registry": {
      const status = str(f.status);
      return {
        actor: "Model registry",
        text: `${modelName(str(f.model_id))} set to ${status} by ${str(f.changed_by)}`,
        chip: { label: status, tone: status === "approved" ? "ok" : "bad" },
        policies: [],
        key: true,
      };
    }
    case "decision": {
      const allow = r.decision === "Allow";
      const routine = allow && ["invoke_model", "read_network", "handoff"].includes(r.action);
      const needsPerson = !allow && f.context && typeof f.context === "object" && (f.context as Record<string, unknown>).human_approved === false;
      return {
        actor: agentName(f.principal),
        text: `Asks to ${toolWords(r.action, str(f.resource))}`,
        sub: needsPerson && r.action !== "restart_all_towers" ? "No person has approved it yet." : undefined,
        chip: { label: allow ? "Allow" : "Deny", tone: allow ? "ok" : "bad" },
        policies: policyList(f.policies),
        key: !routine,
      };
    }
    case "network_read":
      return {
        actor: "Network agent",
        text: `Read the live network from Dynatrace: ${str(f.problem, "no problem")} open, model ${str(f.model_running)} running, ${str(f.phones_dropped, String(num(f.phones_dropped)))} phones cut off`,
        policies: [],
        key: true,
      };
    case "guardrail": {
      const clean = str(f.action_taken) === "NONE";
      return {
        actor: who,
        text: `Bedrock Guardrails screened the ${str(f.source) === "INPUT" ? "prompt" : "answer"}: ${clean ? "clean" : "intervened"}`,
        chip: clean ? undefined : { label: "Intervened", tone: "bad" },
        policies: [],
        key: !clean,
      };
    }
    case "model_call":
      return {
        actor: who,
        text: `Called ${modelName(str(f.model_id))} · ${(num(f.latency_ms) / 1000).toFixed(1)} s · ${(num(f.input_tokens) + num(f.output_tokens)).toLocaleString("en-US")} tokens`,
        policies: [],
        key: false,
      };
    case "handoff":
      return { actor: agentName(f.from_agent), text: `Handed off to the ${agentName(f.to_agent).toLowerCase()}`, sub: str(f.summary) ? `“${str(f.summary)}”` : undefined, policies: [], key: true };
    case "denied":
      return { actor: who, text: `Blocked: ${toolWords(str(f.tool))}. Nothing changed.`, chip: { label: "Denied", tone: "bad" }, policies: policyList(f.policies), key: true };
    case "approval_requested":
      return { actor: who, text: `Asks a person to approve: ${toolWords(str(f.tool))}`, chip: { label: "Waiting", tone: "meh" }, policies: [], key: true };
    case "approval": {
      const yes = f.approved === true;
      const test = str(f.approver) === "test-unattended";
      return {
        actor: "Person",
        text: `${test ? "Unattended test" : str(f.approver)} ${yes ? "approved" : "rejected"}: ${toolWords(str(f.tool))}`,
        sub: test ? "Test run: approved by a flag, no person at the keyboard." : undefined,
        chip: { label: yes ? "Approved" : "Rejected", tone: yes ? (test ? "meh" : "ok") : "bad" },
        policies: [],
        key: true,
      };
    }
    case "executed":
      return { actor: who, text: `Done: ${str(f.result, toolWords(str(f.tool)))}`, chip: { label: "Executed", tone: "ok" }, policies: [], key: true };
    case "refused":
      return {
        actor: who,
        text: `Call to ${modelName(str(f.model_id))} refused: ${str(f.reason)}`,
        chip: { label: "Refused", tone: "bad" },
        policies: [],
        key: true,
      };
    case "judged":
      return f.score === null || f.score === undefined
        ? { actor: "Judge", text: "The judge returned no score.", chip: { label: "No score", tone: "meh" }, policies: [], key: true }
        : {
            actor: "Judge",
            text: `${modelName(str(f.judge_model))} scored the agents ${num(f.score)} of 5`,
            sub: str(f.reason),
            chip: { label: str(f.verdict, "scored"), tone: str(f.verdict) === "pass" ? "ok" : "bad" },
            policies: [],
            key: true,
          };
    case "run_end":
      return { actor: "Checkpoint", text: "Run ended", policies: [], key: true };
    default:
      return { actor: "Checkpoint", text: r.kind.replace(/_/g, " "), policies: [], key: false };
  }
}

/** Counts for one run's summary line. */
export function tally(run: Run) {
  const rs = run.records;
  const decisions = rs.filter((r) => r.kind === "decision");
  const guards = rs.filter((r) => r.kind === "guardrail");
  const calls = rs.filter((r) => r.kind === "model_call");
  return {
    decisions: decisions.length,
    allowed: decisions.filter((r) => r.decision === "Allow").length,
    denied: decisions.filter((r) => r.decision === "Deny").length,
    guards: guards.length,
    intervened: guards.filter((r) => str(r.f.action_taken) !== "NONE").length,
    calls: calls.length,
    tokens: calls.reduce((s, r) => s + num(r.f.input_tokens) + num(r.f.output_tokens), 0),
    approvals: rs.filter((r) => r.kind === "approval").length,
  };
}

/** The newest rollback the agents carried out through the checkpoint, if any. */
export const latestRollback = (records: CpRecord[]): CpRecord | undefined =>
  records.filter((r) => r.kind === "executed" && str(r.f.tool) === "rollback_model").sort((a, b) => b.t - a.t)[0];

/** The Live tab's words when the Track 3 agents, not the Claude ops agent, rolled the network back. */
export function fixStory(records: CpRecord[], fix: CpRecord): { title: string; sub: string } {
  const run = records.filter((r) => r.run === fix.run);
  const denied = run.find((r) => r.kind === "denied");
  const allow = run.find((r) => r.kind === "decision" && r.action === "rollback_model" && r.decision === "Allow");
  const test = str(fix.f.approver) === "test-unattended";
  const policy = policyList(allow?.f.policies).join(", ");
  return {
    title: test ? "The change agent rolled back to v1." : `The change agent rolled back to v1 after ${str(fix.f.approver)} approved.`,
    sub: `${denied ? `The checkpoint blocked “${toolWords(str(denied.f.tool))}”, then` : "The checkpoint"} allowed the rollback only after ${test ? "an approval" : "a person said yes"}${policy ? ` (policy ${policy})` : ""}.`,
  };
}

// ---- the four things Track 3 asks to see -----------------------------------------------------

export interface Proof {
  id: "deny" | "allow" | "pause" | "trace";
  label: string;
  shown: boolean;
  title: string;
  detail: string;
  tone: Tone;
  time: number;
  run: string;
}

const latest = (records: CpRecord[], kind: string) => records.filter((r) => r.kind === kind).sort((a, b) => b.t - a.t)[0];

export function toProofs(records: CpRecord[], runs: Run[]): Proof[] {
  const none = (id: Proof["id"], label: string, title: string): Proof => ({ id, label, shown: false, title, detail: "", tone: "meh", time: 0, run: "" });

  const denied = latest(records, "denied");
  const deny: Proof = denied
    ? {
        id: "deny",
        label: "A live deny",
        shown: true,
        title: `Blocked: ${toolWords(str(denied.f.tool))}`,
        detail: `${agentName(denied.f.who)} · policy ${policyList(denied.f.policies).join(", ")}`,
        tone: "ok",
        time: denied.t,
        run: denied.run,
      }
    : none("deny", "A live deny", "No call denied yet");

  const done = latest(records, "executed");
  let allow = none("allow", "A live allow", "No change made yet");
  if (done) {
    const tool = str(done.f.tool);
    const ok = records.find((r) => r.run === done.run && r.kind === "decision" && r.action === tool && r.decision === "Allow");
    const test = str(done.f.approver) === "test-unattended";
    allow = {
      id: "allow",
      label: "A live allow",
      shown: true,
      title: `Allowed: ${toolWords(tool)}`,
      detail: `${test ? "unattended test approval" : `approved by ${str(done.f.approver)}`} · policy ${policyList(ok?.f.policies).join(", ") || "–"}`,
      tone: test ? "meh" : "ok",
      time: done.t,
      run: done.run,
    };
  }

  const refused = latest(records, "refused");
  const pause: Proof = refused
    ? {
        id: "pause",
        label: "A pause",
        shown: true,
        title: `${modelName(str(refused.f.model_id))} withdrawn, call refused`,
        detail: `${agentName(refused.f.who)} · ${str(refused.f.reason)}`,
        tone: "ok",
        time: refused.t,
        run: refused.run,
      }
    : none("pause", "A pause", "No model withdrawn yet");

  const whole = runs.find((r) => r.complete && r.type === "incident") ?? runs.find((r) => r.complete);
  const trace: Proof = whole
    ? {
        id: "trace",
        label: "A rebuilt trace",
        shown: true,
        title: `${whole.records.length} records, one trace id`,
        detail: `${whole.traceId.slice(0, 8)}… · ${Math.round((whole.end - whole.start) / 1000)} s from first call to last action`,
        tone: "ok",
        time: whole.end,
        run: whole.id,
      }
    : none("trace", "A rebuilt trace", "No complete run yet");

  return [deny, allow, pause, trace];
}
