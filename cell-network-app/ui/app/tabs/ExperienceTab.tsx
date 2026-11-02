import React from "react";
import { Surface } from "@dynatrace/strato-components/layouts";
import { APPS, appWorks, feedSeconds, type AppStats, type Frame } from "../data/model";

interface Props {
  frame?: Frame;
  v1?: AppStats;
  v2?: AppStats;
  showPhones?: boolean;
}

const pct = (n?: number) => (n === undefined ? "–" : `${Math.round(n)}%`);

function insight(v1?: AppStats, v2?: AppStats): string {
  if (!v1 || !v2) return "Run both models to compare what they do to the apps.";
  const callDrop = v1.call - v2.call;
  const reelsGain = v2.reels - v1.reels;
  const reels = Math.abs(reelsGain) < 3 ? "barely changes Instagram Reels" : reelsGain > 0 ? `helps Reels a little (+${Math.round(reelsGain)} pts)` : `hurts Reels too (${Math.round(reelsGain)} pts)`;
  return callDrop > 3
    ? `v2 ${reels}, but WhatsApp calls stop working for ${Math.round(callDrop)} in every 100 phones (${pct(v1.call)} → ${pct(v2.call)}).`
    : `v2 ${reels} and keeps WhatsApp calls working (${pct(v1.call)} → ${pct(v2.call)}).`;
}

/** The network change seen from the phone: can each app do its job right now, and over the last 15 minutes. */
export const ExperienceTab = ({ frame, v1, v2, showPhones = true }: Props) => {
  const phones = frame ? [...frame.phones].sort((a, b) => a.rate - b.rate) : [];
  return (
    <div className="tab">
      <div className="tab-intro">
        <div className="tab-title">What the network change does to the apps</div>
        <div className="muted">
          Radio numbers turned into what people feel: can a phone hold a WhatsApp call, play Reels, load the feed. Dynatrace only sees
          each phone&apos;s speed, never messages or content.
        </div>
      </div>

      <div className="apps">
        {APPS.map((app) => {
          const now = frame ? frame.phones.filter((p) => appWorks(p.rate, app.need)).length : 0;
          const a = v1?.[app.id];
          const b = v2?.[app.id];
          const worse = a !== undefined && b !== undefined && b < a - 3;
          return (
            <Surface key={app.id} elevation="raised" className={`panel app${worse ? " worse" : ""}`}>
              <div className="eyebrow">{app.name}</div>
              <div className="app-now">
                <span className="big-value">{frame ? `${now}/${frame.phones.length}` : "–"}</span>
                <span className="muted small">phones · {app.good} now</span>
              </div>
              <div className="app-bars">
                {(["v1", "v2"] as const).map((m) => {
                  const v = m === "v1" ? a : b;
                  return (
                    <div key={m} className="cmp-bar-row">
                      <span className={`model-tag ${m}`}>{m}</span>
                      <div className="cmp-track">
                        <div className={`cmp-fill ${m}${m === "v2" && worse ? " bad" : ""}`} style={{ width: `${v ?? 0}%` }} />
                      </div>
                      <span className="cmp-value">{pct(v)}</span>
                    </div>
                  );
                })}
              </div>
              <div className="muted small mono">needs ≥ {app.need} Mbps · last 15 min</div>
            </Surface>
          );
        })}
      </div>

      <Surface elevation="raised" className="panel">
        <div className="insight big-insight">{insight(v1, v2)}</div>
      </Surface>

      {showPhones && (
      <Surface elevation="raised" className="panel">
        <div className="eyebrow">Every phone right now{frame ? ` · model ${frame.policy}` : ""}</div>
        <table className="phones">
          <thead>
            <tr>
              <th>Phone</th>
              <th>Speed</th>
              {APPS.map((a) => (
                <th key={a.id}>{a.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {phones.map((p) => (
              <tr key={p.id} className={p.state === "dropped" ? "dropped" : ""}>
                <td className="mono">phone-{String(p.id).padStart(2, "0")}</td>
                <td className="mono">{p.rate > 0 ? `${p.rate.toFixed(1)} Mbps` : p.state === "dropped" ? "cut off" : "no signal"}</td>
                {APPS.map((a) => {
                  const ok = appWorks(p.rate, a.need);
                  const label =
                    a.id === "feed" && p.rate > 0 ? `${Math.min(99, feedSeconds(p.rate)).toFixed(1)} s` : ok ? "works" : p.rate > 0 ? "struggles" : "fails";
                  return (
                    <td key={a.id}>
                      <span className={`chip ${ok ? "ok" : p.rate > 0 ? "meh" : "bad"}`}>{label}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        <div className="muted small">Thresholds are rules of thumb. The simulator&apos;s data rate is read as Mbps.</div>
      </Surface>
      )}
    </div>
  );
};
