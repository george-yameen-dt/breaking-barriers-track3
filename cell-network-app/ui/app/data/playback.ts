import { useCallback, useEffect, useRef } from "react";
import type { Row } from "./hooks";
import { toFrame, type Frame } from "./model";

/** Stay this far behind the newest step so there is always a next step to move toward. */
const LAG_MS = 4000;
const KEEP_MS = 60_000;

export interface Sample {
  a: Frame;
  b: Frame;
  /** 0 = at step a, 1 = at step b. */
  alpha: number;
  /** Wall-clock ms since new steps last arrived from Grail. */
  sinceData: number;
  /** How far the replay runs behind the simulator. */
  behindMs: number;
}

/**
 * Buffers simulator steps as they arrive from Grail and replays them at the pace they were recorded,
 * a few seconds behind, so phones glide instead of jumping every poll.
 */
export function useFramePlayer(rows: Row[], fallback: Frame | undefined) {
  const buf = useRef({ frames: [] as Frame[], delay: -1, arrived: 0 });

  useEffect(() => {
    const b = buf.current;
    const lastT = b.frames.length ? b.frames[b.frames.length - 1].t : 0;
    const fresh = rows.map(toFrame).filter((f): f is Frame => f !== undefined && f.t > lastT);
    if (!fresh.length) return;
    const now = Date.now();
    b.frames.push(...fresh);
    const newest = b.frames[b.frames.length - 1].t;
    b.frames = b.frames.filter((f) => f.t >= newest - KEEP_MS);
    // Delay = how far the replay runs behind the browser clock. It also absorbs clock skew with the laptop.
    const measured = now - newest + LAG_MS;
    b.delay = b.delay < 0 || Math.abs(measured - b.delay) > 5000 ? measured : b.delay * 0.8 + measured * 0.2;
    b.arrived = now;
  }, [rows]);

  return useCallback(
    (now: number): Sample | undefined => {
      const { frames, delay, arrived } = buf.current;
      const sinceData = arrived ? now - arrived : Number.POSITIVE_INFINITY;
      const behindMs = delay;
      if (!frames.length) return fallback ? { a: fallback, b: fallback, alpha: 1, sinceData, behindMs } : undefined;
      const t = now - delay;
      const i = frames.findIndex((f) => f.t > t);
      if (i === -1) {
        const last = frames[frames.length - 1];
        return { a: last, b: last, alpha: 1, sinceData, behindMs };
      }
      if (i === 0) return { a: frames[0], b: frames[0], alpha: 1, sinceData, behindMs };
      const a = frames[i - 1];
      const b = frames[i];
      // A new episode puts every phone somewhere new: cut, don't slide.
      if (a.episode !== b.episode || b.step < a.step) return { a: b, b, alpha: 1, sinceData, behindMs };
      const alpha = Math.min(1, Math.max(0, (t - a.t) / Math.max(1, b.t - a.t)));
      return { a, b, alpha, sinceData, behindMs };
    },
    [fallback],
  );
}
