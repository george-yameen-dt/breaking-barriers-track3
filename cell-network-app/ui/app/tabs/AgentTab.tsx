import React from "react";
import { Surface } from "@dynatrace/strato-components/layouts";
import { num, str, type Row } from "../data/hooks";

interface Props {
  runs: Row[];
  drops: Row[];
}

const time = (t: number) => new Date(t).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" });

/** Did the rollback work? No dropped phones in the 30–120 s after the agent acted (the time it takes for steps to land). */
function verdict(run: Row, drops: Row[]): "fixed" | "not fixed" | "checking" | "no action" {
  const acted = run["agent.acted"] === true || str(run["agent.acted"]) === "true";
  if (!acted) return "no action";
  const t = Date.parse(str(run.timestamp));
  const after = drops.filter((d) => {
    const b = Date.parse(str(d.bucket));
    return b > t + 30_000 && b <= t + 120_000;
  });
  if (after.length < 3) return "checking";
  return after.every((d) => num(d.dropped) === 0) ? "fixed" : "not fixed";
}

/** The AI ops agent: what it is doing, every answer it gave, and whether its fixes worked. */
export const AgentTab = ({ runs, drops }: Props) => {
  const acted = runs.filter((r) => r["agent.acted"] === true || str(r["agent.acted"]) === "true").length;
  const avg = (key: string) => (runs.length ? runs.reduce((s, r) => s + num(r[key]), 0) / runs.length : 0);
  const tokens = runs.reduce((s, r) => s + num(r["agent.input_tokens"]) + num(r["agent.output_tokens"]), 0);
  const fixed = runs.filter((r) => verdict(r, drops) === "fixed").length;
  return (
    <div className="tab">
      <div className="tab-intro">
        <div className="tab-title">AI ops agent</div>
        <div className="muted">
          When Dynatrace opens a problem, the agent reads the live network from Grail, asks Claude what happened, and rolls back if
          needed. Every step is traced: in Arize today (project cell-network-ops-agent), and in Dynatrace AI Observability once a
          traces token is added.
        </div>
      </div>

      <div className="bigs four">
        <Surface elevation="raised" className="big">
          <div className="eyebrow">Problems handled · 2 h</div>
          <div className="big-value">{runs.length}</div>
          <div className="muted small">{acted} rolled back</div>
        </Surface>
        <Surface elevation="raised" className="big">
          <div className="eyebrow">Fixes that worked</div>
          <div className="big-value">{acted ? `${fixed}/${acted}` : "–"}</div>
          <div className="muted small">no dropped phones after the rollback</div>
        </Surface>
        <Surface elevation="raised" className="big">
          <div className="eyebrow">Time to answer</div>
          <div className="big-value">{runs.length ? `${(avg("agent.duration_ms") / 1000).toFixed(1)} s` : "–"}</div>
          <div className="muted small">Claude, average</div>
        </Surface>
        <Surface elevation="raised" className="big">
          <div className="eyebrow">Cost</div>
          <div className="big-value">{runs.length ? `$${avg("agent.cost_usd").toFixed(4)}` : "–"}</div>
          <div className="muted small">per problem · {tokens.toLocaleString("en-US")} tokens total</div>
        </Surface>
      </div>

      <Surface elevation="raised" className="panel">
        <div className="eyebrow">Every answer · last 2 hours</div>
        {runs.length ? (
          <table className="phones runs">
            <thead>
              <tr>
                <th>Time</th>
                <th>Problem</th>
                <th>What Claude said</th>
                <th>Action</th>
                <th>Worked?</th>
                <th>Tokens</th>
                <th>Cost</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => {
                const v = verdict(r, drops);
                return (
                  <tr key={str(r.timestamp)}>
                    <td className="mono">{time(Date.parse(str(r.timestamp)))}</td>
                    <td className="mono">{str(r["agent.problem"])}</td>
                    <td>{str(r.content)}</td>
                    <td>{str(r["agent.action"]) === "rollback_to_v1" ? "Roll back to v1" : "Keep watching"}</td>
                    <td>
                      <span className={`chip ${v === "fixed" ? "ok" : v === "not fixed" ? "bad" : "meh"}`}>{v}</span>
                    </td>
                    <td className="mono">{(num(r["agent.input_tokens"]) + num(r["agent.output_tokens"])).toLocaleString("en-US")}</td>
                    <td className="mono">${num(r["agent.cost_usd"]).toFixed(4)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="muted">No answers yet. Start ./run-demo.sh and the first one comes about 2 minutes after model v2 goes live.</div>
        )}
      </Surface>
    </div>
  );
};
