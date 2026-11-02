const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export const fmtCompact = (n: number): string => compact.format(n);
export const fmtUsd = (n: number): string => `$${compact.format(n)}`;
export const fmtPct = (n: number, digits = 0): string => `${n.toFixed(digits)}%`;
export const pct = (part: number, total: number): number => (total > 0 ? (part * 100) / total : 0);

/** 90_000 -> "2 min" */
export function fmtAgo(ms: number): string {
  if (ms < 60_000) return `${Math.max(0, Math.round(ms / 1000))} s`;
  if (ms < 3_600_000) return `${Math.round(ms / 60_000)} min`;
  return `${(ms / 3_600_000).toFixed(1)} h`;
}

export const fmtClock = (t: number): string =>
  new Date(t).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });

/** "needs_attention" -> "needs attention" */
export const words = (s: string): string => s.replace(/_/g, " ");
