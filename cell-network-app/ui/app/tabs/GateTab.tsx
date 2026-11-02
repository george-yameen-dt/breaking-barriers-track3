import React from "react";
import { Surface } from "@dynatrace/strato-components/layouts";
import type { AppStats, PolicyStats } from "../data/model";

interface Props {
  v1?: PolicyStats;
  v2?: PolicyStats;
  a1?: AppStats;
  a2?: AppStats;
}

interface Check {
  name: string;
  target: string;
  v1?: number;
  v2?: number;
  fmt: (n: number) => string;
  pass: (n: number) => boolean;
}

/** Should v2 replace v1? The checks a release gate (Site Reliability Guardian) would run, on the last 15 minutes. */
export const GateTab = ({ v1, v2, a1, a2 }: Props) => {
  const checks: Check[] = [
    { name: "Safety: no phone left without signal", target: "0 per step", v1: v1?.dropped, v2: v2?.dropped, fmt: (n) => n.toFixed(1), pass: (n) => n < 0.05 },
    { name: "Coverage: phones served", target: "≥ 95%", v1: v1?.servedPct, v2: v2?.servedPct, fmt: (n) => `${n.toFixed(0)}%`, pass: (n) => n >= 95 },
    { name: "App experience: WhatsApp calls work", target: "≥ 90% of phones", v1: a1?.call, v2: a2?.call, fmt: (n) => `${n.toFixed(0)}%`, pass: (n) => n >= 90 },
  ];
  const ready = v2 !== undefined && a2 !== undefined;
  const failed = checks.filter((c) => c.v2 !== undefined && !c.pass(c.v2));
  const ship = ready && failed.length === 0;
  const faster = v1 && v2 && v1.rate > 0 ? v2.rate / v1.rate : 0;
  return (
    <div className="tab">
      <div className="tab-intro">
        <div className="tab-title">Release gate: should model v2 replace v1?</div>
        <div className="muted">
          The checks a risk owner wants before a new model goes live, run on the last 15 minutes of real simulator data. This is what a
          Dynatrace Site Reliability Guardian automates.
        </div>
      </div>

      <Surface elevation="raised" className={`verdict ${!ready ? "" : ship ? "ship" : "block"}`}>
        <div className="eyebrow">Verdict for v2</div>
        <div className="verdict-text">{!ready ? "Not enough data on v2 yet" : ship ? "Ship it" : "Don't ship"}</div>
        <div className="small">
          {!ready
            ? "Run v2 for a minute to score it."
            : ship
              ? "v2 passes every check."
              : `Fails ${failed.length} of ${checks.length} checks${faster > 1.1 ? `, even though it is ${faster.toFixed(1)}x faster for the phones it does serve` : ""}.`}
        </div>
      </Surface>

      <Surface elevation="raised" className="panel">
        <table className="phones gate">
          <thead>
            <tr>
              <th>Check</th>
              <th>Target</th>
              <th>v1 (today)</th>
              <th>v2 (candidate)</th>
            </tr>
          </thead>
          <tbody>
            {checks.map((c) => (
              <tr key={c.name}>
                <td>{c.name}</td>
                <td className="mono">{c.target}</td>
                {[c.v1, c.v2].map((v, i) => (
                  <td key={i}>
                    {v === undefined ? (
                      "–"
                    ) : (
                      <span className={`chip ${c.pass(v) ? "ok" : "bad"}`}>
                        {c.fmt(v)} · {c.pass(v) ? "pass" : "fail"}
                      </span>
                    )}
                  </td>
                ))}
              </tr>
            ))}
            <tr className="info">
              <td>Speed for served phones (info only)</td>
              <td className="mono">higher is better</td>
              <td className="mono">{v1 ? v1.rate.toFixed(1) : "–"}</td>
              <td className="mono">{v2 ? v2.rate.toFixed(1) : "–"}</td>
            </tr>
          </tbody>
        </table>
      </Surface>
    </div>
  );
};
