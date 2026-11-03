import React, { useMemo, useState } from "react";
import { Surface } from "@dynatrace/strato-components/layouts";
import { Button } from "@dynatrace/strato-components/buttons";
import { ToggleButtonGroup } from "@dynatrace/strato-components/forms";
import { sendIntent } from "@dynatrace-sdk/navigation";
import { useQuery } from "../data/hooks";
import { fmtAgo } from "../data/format";
import { problemByDisplayId, runRecords } from "../data/queries";
import { openProblem, toProblem } from "../data/platform";
import { describe, tally, type Proof, type Run } from "../data/checkpoint";

interface Props {
  runs: Run[];
  proofs: Proof[];
  now: number;
}

const time = (t: number) => new Date(t).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" });

const RUN_TYPES: Record<string, string> = { incident: "Incident", pause: "Pause", setup: "Setup" };

/** The Track 3 checkpoint: the four things it must show, then one run's trace, record by record. */
export const CheckpointTab = ({ runs, proofs, now }: Props) => {
  const [picked, setPicked] = useState<string>();
  const [view, setView] = useState("key");
  // Until someone picks a run, follow the newest incident as it comes in.
  const run = runs.find((r) => r.id === picked) ?? runs.find((r) => r.type === "incident") ?? runs[0];
  const prob = useQuery(problemByDisplayId(run?.problem ?? ""), 30_000, Boolean(run?.problem));
  // Only the problem this run read: a disabled query can still hold the last run's answer.
  const found = useMemo(() => toProblem(prob.first), [prob.first]);
  const problem = run?.problem && found?.displayId === run.problem ? found : undefined;
  const steps = useMemo(() => (run ? run.records.map((r) => ({ r, s: describe(r) })) : []), [run]);
  const shown = view === "key" ? steps.filter((x) => x.s.key) : steps;
  const t = run ? tally(run) : undefined;
  const recent = runs.slice(0, 6);

  return (
    <div className="tab">
      <div className="tab-intro">
        <div className="tab-title">Checkpoint</div>
        <div className="muted">
          Every model call, hand-off and tool call the agents make crosses one checkpoint. A Cedar policy decides, Bedrock Guardrails
          screen the text, a person approves any change to the network, and each step is recorded under one trace id. This page reads
          those records back from Grail.
        </div>
      </div>

      <div className="bigs four">
        {proofs.map((p, i) => (
          <Surface
            key={p.id}
            elevation="raised"
            className={`big proof ${p.shown ? p.tone : "none"}${run?.id === p.run ? " picked" : ""}`}
            onClick={p.run ? () => setPicked(p.run) : undefined}
          >
            <div className="eyebrow">
              {i + 1} · {p.label}
            </div>
            <div className="proof-title">{p.title}</div>
            <div className="muted small">{p.detail}</div>
            <div className="proof-foot">
              <span className={`chip ${p.shown ? p.tone : "meh"}`}>{p.shown ? (p.tone === "meh" ? "Shown in a test" : "Shown") : "Not yet"}</span>
              {p.shown && <span className="muted small">{fmtAgo(now - p.time)} ago</span>}
            </div>
          </Surface>
        ))}
      </div>

      <Surface elevation="raised" className="panel trace">
        <div className="trace-head">
          <div className="eyebrow">Follow one trace</div>
          {recent.length > 1 && (
            <ToggleButtonGroup value={run?.id ?? ""} onChange={(v) => setPicked(v)}>
              {recent.map((r) => (
                <ToggleButtonGroup.Item key={r.id} value={r.id}>
                  {RUN_TYPES[r.type] ?? r.type} · {time(r.start)}
                </ToggleButtonGroup.Item>
              ))}
            </ToggleButtonGroup>
          )}
        </div>

        {run && t ? (
          <>
            <div className="trace-meta">
              <span className={`chip ${run.complete ? "ok" : run.gap === "still running" ? "meh" : "bad"}`}>
                {run.complete ? "Complete" : run.gap}
              </span>
              <span className="mono">trace {run.traceId}</span>
              <span className="muted">
                {run.records.length} records · {time(run.start)} · {Math.round((run.end - run.start) / 1000)} s
              </span>
              <span className="trace-actions">
                {problem && (
                  <Button variant="default" onClick={() => openProblem(problem)}>
                    Open {problem.displayId}
                  </Button>
                )}
                <Button variant="default" onClick={() => sendIntent({ "dt.query": runRecords(run.id) })}>
                  See the data
                </Button>
              </span>
            </div>
            <div className="trace-tally">
              <span>
                <strong>{t.decisions}</strong> policy decisions ({t.allowed} allowed, {t.denied} denied)
              </span>
              <span>
                <strong>{t.calls}</strong> model calls · {t.tokens.toLocaleString("en-US")} tokens
              </span>
              <span>
                <strong>{t.guards}</strong> guardrail checks · {t.intervened} intervened
              </span>
              <span>
                <strong>{t.approvals}</strong> {t.approvals === 1 ? "approval" : "approvals"}
              </span>
            </div>

            <div className="trace-view">
              <ToggleButtonGroup value={view} onChange={(v) => setView(v)}>
                <ToggleButtonGroup.Item value="key">Key steps</ToggleButtonGroup.Item>
                <ToggleButtonGroup.Item value="all">Every record</ToggleButtonGroup.Item>
              </ToggleButtonGroup>
              {view === "key" && steps.length > shown.length && (
                <span className="muted small">{steps.length - shown.length} routine records hidden: allowed model calls and clean guardrail checks</span>
              )}
            </div>

            <table className="phones steps">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Time</th>
                  <th>Who</th>
                  <th>What happened</th>
                  <th>Decision</th>
                  <th>Policy</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(({ r, s }) => (
                  <tr key={r.seq} className={s.chip?.tone === "bad" ? "denied" : undefined}>
                    <td className="mono">{r.seq}</td>
                    <td className="mono">+{Math.round((r.t - run.start) / 1000)} s</td>
                    <td className="who">{s.actor}</td>
                    <td>
                      <div>{s.text}</div>
                      {s.sub && <div className="muted small">{s.sub}</div>}
                    </td>
                    <td>{s.chip && <span className={`chip ${s.chip.tone}`}>{s.chip.label}</span>}</td>
                    <td className="mono small">{s.policies.join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <div className="muted">No checkpoint records in the last 6 hours. Run ./swarm.sh run --cloud --fault outage-C in breaking-barriers/track3 and they show up here about 20 s later.</div>
        )}
      </Surface>
    </div>
  );
};
