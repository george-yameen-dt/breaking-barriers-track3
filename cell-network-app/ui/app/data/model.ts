import { num, str, type Row } from "./hooks";

export type PhoneState = "served" | "dropped" | "no signal" | "searching";

export interface Phone {
  id: number;
  x: number;
  y: number;
  state: PhoneState;
  links: number[];
  /** Quality of experience scaled to -1..1, as the simulator scores it. */
  util: number;
  rate: number;
}

export interface Tower {
  id: number;
  name: string;
  x: number;
  y: number;
  load: number;
  range: number;
}

export interface Frame {
  t: number;
  policy: string;
  policyName: string;
  episode: number;
  step: number;
  active: number;
  served: number;
  dropped: number;
  rate: number;
  w: number;
  h: number;
  phones: Phone[];
  towers: Tower[];
  /** Story phase when the simulator runs with --story: healthy, broken or fixed. */
  story: string;
}

function parseList(value: unknown): Row[] {
  if (typeof value !== "string") return [];
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? (parsed as Row[]) : [];
  } catch {
    return [];
  }
}

const STATES: PhoneState[] = ["served", "dropped", "no signal", "searching"];

export function toFrame(r: Row): Frame | undefined {
  const t = Date.parse(str(r.timestamp));
  if (Number.isNaN(t)) return undefined;
  const phones = parseList(r.ues).map((u) => {
    const state = str(u.state) as PhoneState;
    return {
      id: num(u.id),
      x: num(u.x),
      y: num(u.y),
      state: STATES.includes(state) ? state : "searching",
      links: Array.isArray(u.links) ? u.links.map((l) => num(l)) : [],
      util: num(u.util, -1),
      rate: num(u.rate),
    };
  });
  const towers = parseList(r.towers).map((b) => ({
    id: num(b.id),
    name: str(b.name, String(num(b.id))),
    x: num(b.x),
    y: num(b.y),
    load: num(b.load),
    range: num(b.range),
  }));
  return {
    t,
    policy: str(r.policy, "v1"),
    policyName: str(r.policyName),
    episode: num(r.episode),
    step: num(r.step),
    active: num(r.active),
    served: num(r.served),
    dropped: num(r.dropped),
    rate: num(r.rate),
    w: num(r.w, 200),
    h: num(r.h, 300),
    phones,
    towers,
    story: str(r.story),
  };
}

/** How good a served phone's connection is, in three bands the map colors by. */
export function quality(p: Phone): "good" | "fair" | "poor" | "off" {
  if (p.state !== "served") return "off";
  if (p.util >= 0.15) return "good";
  if (p.util >= -0.25) return "fair";
  return "poor";
}

export interface PolicyStats {
  policy: string;
  steps: number;
  dropped: number;
  servedPct: number;
  rate: number;
}

export function toStats(rows: Row[]): Map<string, PolicyStats> {
  const out = new Map<string, PolicyStats>();
  for (const r of rows) {
    const policy = str(r.policy);
    if (!policy) continue;
    out.set(policy, { policy, steps: num(r.steps), dropped: num(r.dropped), servedPct: num(r.servedPct), rate: num(r.rate) });
  }
  return out;
}

/** Towers carrying no phones while a dropped phone sits inside their coverage. */
export function idleTowers(f: Frame): number {
  const dropped = f.phones.filter((p) => p.state === "dropped");
  return f.towers.filter((t) => t.load === 0 && dropped.some((p) => Math.hypot(p.x - t.x, p.y - t.y) <= t.range)).length;
}

/**
 * What each app needs from the network, as a rule of thumb. The simulator's data rate is read as Mbps.
 * Feed: a 2.5 MB Instagram feed page loads in under 2.5 s at 8 Mbps.
 */
export const APPS = [
  { id: "call", name: "WhatsApp call", need: 0.1, good: "clear voice" },
  { id: "video", name: "WhatsApp video call", need: 1.5, good: "smooth video" },
  { id: "reels", name: "Instagram Reels", need: 3, good: "no buffering" },
  { id: "feed", name: "Instagram feed", need: 8, good: "loads in under 2.5 s" },
] as const;

export type AppId = (typeof APPS)[number]["id"];

export const appWorks = (rate: number, need: number) => rate >= need;

/** Seconds to load a 2.5 MB feed page (20 megabits) at this rate. */
export const feedSeconds = (rate: number) => (rate > 0 ? 20 / rate : Infinity);

export interface AppStats {
  policy: string;
  phoneSteps: number;
  call: number;
  video: number;
  reels: number;
  feed: number;
  noSignal: number;
}

export function toAppStats(rows: Row[]): Map<string, AppStats> {
  const out = new Map<string, AppStats>();
  for (const r of rows) {
    const policy = str(r.policy);
    if (!policy) continue;
    out.set(policy, {
      policy,
      phoneSteps: num(r.phoneSteps),
      call: num(r.call),
      video: num(r.video),
      reels: num(r.reels),
      feed: num(r.feed),
      noSignal: num(r.noSignal),
    });
  }
  return out;
}
