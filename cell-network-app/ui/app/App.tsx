import React, { useEffect, useMemo, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { AppHeader, PageLayout } from "@dynatrace/strato-components/layouts";
import { Button } from "@dynatrace/strato-components/buttons";
import { MessageContainer, Skeleton } from "@dynatrace/strato-components/content";
import { str, useQuery } from "./data/hooks";
import { fmtAgo } from "./data/format";
import { agentAdvice, agentRuns, cellProblem, checkpointRecords, comparison, dropsHistory, experience, framesSince, lastFrame } from "./data/queries";
import { toAppStats, toFrame, toStats, type Frame } from "./data/model";
import { useFramePlayer, type Sample } from "./data/playback";
import { openProblem, toProblem, type Problem } from "./data/platform";
import { fixStory, latestRollback, toProofs, toRecords, toRuns } from "./data/checkpoint";
import { CellMap } from "./components/CellMap";
import { ExperienceTab } from "./tabs/ExperienceTab";
import { AgentTab } from "./tabs/AgentTab";
import { GateTab } from "./tabs/GateTab";
import { CheckpointTab } from "./tabs/CheckpointTab";

type Phase = "healthy" | "broken" | "fixed";

const LIVE_MS = 10_000;
/** Never ask Grail for more than this much history when the simulator has been off. */
const MAX_BACK_MS = 120_000;
const isoAgo = (ms: number) => new Date(Date.now() - ms).toISOString();

/** Re-reads the player ten times a second for the words next to the map. */
function useSample(sample: (now: number) => Sample | undefined): Sample | undefined {
  const [s, setS] = useState<Sample | undefined>();
  useEffect(() => {
    const timer = setInterval(() => setS(sample(Date.now())), 100);
    return () => clearInterval(timer);
  }, [sample]);
  return s;
}

const STEPS: { phase: Phase; label: string }[] = [
  { phase: "healthy", label: "Healthy" },
  { phase: "broken", label: "Broken" },
  { phase: "fixed", label: "Fixed by AI" },
];

const Progress = ({ phase }: { phase: Phase }) => {
  const at = STEPS.findIndex((s) => s.phase === phase);
  return (
    <div className="progress" aria-label="Where the story is">
      {STEPS.map((s, i) => (
        <span key={s.phase} className={`progress-step ${s.phase}${i === at ? " now" : i < at ? " done" : ""}`}>
          <span className="progress-dot" />
          {s.label}
        </span>
      ))}
    </div>
  );
};

/** The one sentence that tells the room what is happening right now. */
function headline(
  phase: Phase,
  f: Frame | undefined,
  problem: Problem | undefined,
  quote: string,
  fix?: { title: string; sub: string },
): { title: string; sub: string } {
  if (!f) return { title: "Waiting for the simulator", sub: "Start it on the laptop with ./run-demo.sh." };
  const callsFail = f.phones.filter((p) => p.rate < 0.1).length;
  const calls = `WhatsApp calls fail on ${callsFail} of ${f.phones.length} phones.`;
  if (phase === "fixed" && fix) return fix;
  if (phase === "fixed") {
    return {
      title: f.dropped === 0 ? "Claude rolled back to v1. Every phone is back." : "Claude rolled back to v1.",
      sub: quote ? `“${quote}”` : "The AI agent read the problem and switched back to the safe model.",
    };
  }
  if (phase === "broken") {
    if (f.dropped === 0) return { title: "Model v2 is live.", sub: "A new model now decides which tower each phone uses." };
    return {
      title: `Model v2 cut off ${f.dropped} phone${f.dropped === 1 ? "" : "s"}.`,
      sub: problem?.active ? `Dynatrace opened ${problem.displayId} on those phones. ${calls}` : `Dynatrace is spotting it now. ${calls}`,
    };
  }
  return {
    title: f.dropped === 0 ? "Every phone in range has signal." : `${f.dropped} phones without signal.`,
    sub: "Model v1 picks a tower for each of the 15 phones. Dynatrace watches every step.",
  };
}

export const App = () => {
  const { pathname } = useLocation();
  const tab = pathname.startsWith("/details") ? "details" : pathname.startsWith("/checkpoint") ? "checkpoint" : "live";

  // Only fetch steps we haven't seen yet. The cursor trails the newest step a little in case logs land out of order.
  const [cursor, setCursor] = useState(() => isoAgo(MAX_BACK_MS));
  const recent = useQuery(framesSince(cursor), 2_000);
  useEffect(() => {
    const newest = recent.rows.reduce((m, r) => Math.max(m, Date.parse(str(r.timestamp)) || 0), 0);
    if (newest) setCursor((c) => (newest - 3_000 > Date.parse(c) ? new Date(newest - 3_000).toISOString() : c));
  }, [recent.rows]);
  useEffect(() => {
    const timer = setInterval(() => setCursor((c) => (Date.parse(c) < Date.now() - MAX_BACK_MS * 2 ? isoAgo(MAX_BACK_MS) : c)), 30_000);
    return () => clearInterval(timer);
  }, []);
  const last = useQuery(lastFrame, 60_000);
  const prob = useQuery(cellProblem, 10_000);
  const agent = useQuery(agentAdvice, 10_000);
  const cmp = useQuery(comparison, 30_000);
  const exp = useQuery(experience, 30_000);
  const runs = useQuery(agentRuns, 30_000);
  const drops = useQuery(dropsHistory, 60_000);
  const cp = useQuery(checkpointRecords, 5_000);

  const problem = useMemo(() => toProblem(prob.first), [prob.first]);
  const fallback = useMemo(() => toFrame(last.first), [last.first]);
  const sample = useFramePlayer(recent.rows, fallback);
  const s = useSample(sample);
  const stats = useMemo(() => toStats(cmp.rows), [cmp.rows]);
  const appStats = useMemo(() => toAppStats(exp.rows), [exp.rows]);
  const cpRecords = useMemo(() => toRecords(cp.rows), [cp.rows]);
  const cpRuns = useMemo(() => toRuns(cpRecords, Date.now()), [cpRecords]);
  const proofs = useMemo(() => toProofs(cpRecords, cpRuns), [cpRecords, cpRuns]);

  const now = Date.now();
  const frame = s?.b;
  const live = s !== undefined && s.sinceData < LIVE_MS;
  const model = frame?.policy ?? "v1";
  // The agent's latest answer counts if it came in the last 10 minutes.
  const adviceAt = Date.parse(str(agent.first.timestamp));
  const advice = !Number.isNaN(adviceAt) && now - adviceAt < 10 * 60_000 ? agent.first : undefined;
  // A rollback the Track 3 agents made through the checkpoint in the last 10 minutes, unless the ops agent answered since.
  const rollback = latestRollback(cpRecords);
  const fix = rollback && now - rollback.t < 10 * 60_000 && !(advice && adviceAt > rollback.t) ? fixStory(cpRecords, rollback) : undefined;
  const acted = fix !== undefined || (advice !== undefined && (advice["agent.acted"] === true || str(advice["agent.acted"]) === "true"));
  // The simulator tells us the story step in --story mode; otherwise work it out from the model and the agent.
  const phase: Phase = ["healthy", "broken", "fixed"].includes(frame?.story ?? "")
    ? (frame?.story as Phase)
    : model === "v2"
      ? "broken"
      : acted
        ? "fixed"
        : "healthy";
  const h = headline(phase, frame, problem, phase === "fixed" && advice ? str(advice.content) : "", fix);
  const error = recent.error ?? last.error;

  return (
    <PageLayout>
      <PageLayout.Header>
        <AppHeader>
          <AppHeader.Navigation>
            <AppHeader.Logo as={Link} to="/" />
            <AppHeader.NavigationItem as={Link} to="/" isSelected={tab === "live"}>
              Live
            </AppHeader.NavigationItem>
            <AppHeader.NavigationItem as={Link} to="/details" isSelected={tab === "details"}>
              Details
            </AppHeader.NavigationItem>
            <AppHeader.NavigationItem as={Link} to="/checkpoint" isSelected={tab === "checkpoint"}>
              Checkpoint
            </AppHeader.NavigationItem>
          </AppHeader.Navigation>
        </AppHeader>
      </PageLayout.Header>
      <PageLayout.Content>
        <div className="page">
          {error && (
            <MessageContainer variant="critical">
              <MessageContainer.Title>Simulator data could not be loaded</MessageContainer.Title>
              <MessageContainer.Description>{error}</MessageContainer.Description>
            </MessageContainer>
          )}

          {tab === "live" ? (
            <div className="live">
              <div className="stage">{frame ? <CellMap sample={sample} /> : <Skeleton width="100%" height="560px" />}</div>
              <div className={`story-col ${phase}`}>
                <Progress phase={phase} />
                <div className="headline">{h.title}</div>
                <div className="subline">{h.sub}</div>
                {problem?.active && (
                  <div>
                    <Button variant="emphasized" color={phase === "fixed" ? "neutral" : "critical"} onClick={() => openProblem(problem)}>
                      Open {problem.displayId} in Dynatrace
                    </Button>
                  </div>
                )}
                <div className="foot">
                  <span>
                    <span className="swatch good" /> has signal
                  </span>
                  <span>
                    <span className="swatch dropped" /> cut off
                  </span>
                  <span className="muted">
                    {frame ? (live ? `Model ${model} · about ${Math.round((s?.behindMs ?? 0) / 1000)} s behind the laptop` : `Simulator paused · last step ${fmtAgo(now - frame.t)} ago`) : ""}
                  </span>
                </div>
              </div>
            </div>
          ) : tab === "checkpoint" ? (
            <div className="details">
              <CheckpointTab runs={cpRuns} proofs={proofs} now={now} />
            </div>
          ) : (
            <div className="details">
              <GateTab v1={stats.get("v1")} v2={stats.get("v2")} a1={appStats.get("v1")} a2={appStats.get("v2")} />
              <ExperienceTab frame={frame} v1={appStats.get("v1")} v2={appStats.get("v2")} showPhones={false} />
              <AgentTab runs={runs.rows} drops={drops.rows} />
            </div>
          )}
        </div>
      </PageLayout.Content>
    </PageLayout>
  );
};
