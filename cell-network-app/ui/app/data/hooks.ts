import { useDql } from "@dynatrace-sdk/react-hooks";

export type Row = Record<string, unknown>;

const EMPTY: Row = {};

export function num(value: unknown, fallback = 0): number {
  if (typeof value === "number") return Number.isFinite(value) ? value : fallback;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  return fallback;
}

export function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

/** Runs a DQL query and re-runs it every `refreshMs`. With `enabled` false it waits and returns no rows. */
export function useQuery(query: string, refreshMs: number, enabled = true) {
  const { data, error, isLoading } = useDql(query, { refetchInterval: refreshMs, enabled });
  const rows: Row[] = data?.records ?? [];
  return {
    rows,
    first: rows[0] ?? EMPTY,
    loading: isLoading,
    error: error?.message,
  };
}
