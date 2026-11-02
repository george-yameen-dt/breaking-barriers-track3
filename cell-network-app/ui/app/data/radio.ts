import type { Tower } from "./model";

// The simulator's own radio model (mobile-env defaults: Okumura-Hata path loss, 2.5 GHz, 30 dBm, 50 m masts),
// so the coverage shading shows the same signal strength the model sees.
const FREQ = 2500;
const TX_DBM = 30;
const BS_HEIGHT = 50;
const UE_HEIGHT = 1.5;
const NOISE = 1e-9;
const SNR_THRESHOLD = 2e-8;

const ch = 0.8 + (1.1 * Math.log10(FREQ) - 0.7) * UE_HEIGHT - 1.56 * Math.log10(FREQ);
const LOSS_BASE = 69.55 - ch + 26.16 * Math.log10(FREQ) - 13.82 * Math.log10(BS_HEIGHT);
const LOSS_SLOPE = 44.9 - 6.55 * Math.log10(BS_HEIGHT);

export function snr(distance: number): number {
  const loss = LOSS_BASE + LOSS_SLOPE * Math.log10(distance + 1e-16);
  return 10 ** ((TX_DBM - loss) / 10) / NOISE;
}

/** Signal margin above what a phone needs to connect, in dB. Below 0 means no service. */
export function marginDb(distance: number): number {
  return 10 * Math.log10(snr(distance) / SNR_THRESHOLD);
}

/**
 * Paints best-tower signal strength over the map into an image, once per tower layout.
 * The map is drawn on its side, so image x runs along simulator y.
 */
export function coverageImage(towers: Tower[], w: number, h: number, pxPerUnit: number, rgb: [number, number, number]): string {
  const step = 2; // one computed cell = 2 x 2 simulator units
  const cols = Math.ceil(h / step);
  const rows = Math.ceil(w / step);
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(h * pxPerUnit);
  canvas.height = Math.round(w * pxPerUnit);
  const ctx = canvas.getContext("2d");
  if (!ctx) return "";
  const cell = step * pxPerUnit;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const simY = (c + 0.5) * step;
      const simX = (r + 0.5) * step;
      let best = -Infinity;
      for (const t of towers) best = Math.max(best, marginDb(Math.hypot(simX - t.x, simY - t.y)));
      if (best <= 0) continue;
      const strength = Math.min(1, best / 40);
      ctx.fillStyle = `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${(0.02 + strength * 0.12).toFixed(3)})`;
      ctx.fillRect(c * cell, r * cell, Math.ceil(cell), Math.ceil(cell));
    }
  }
  return canvas.toDataURL("image/png");
}
