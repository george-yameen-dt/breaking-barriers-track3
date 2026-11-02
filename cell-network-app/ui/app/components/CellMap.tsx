import React, { useEffect, useMemo, useState } from "react";
import { quality, type Phone, type Tower } from "../data/model";
import { coverageImage } from "../data/radio";
import type { Sample } from "../data/playback";

/** Simulator units to pixels. The map is drawn turned on its side so the tall 200 x 300 area fills a wide screen. */
const S = 3.4;
const PAD = 34;

interface Props {
  sample: (now: number) => Sample | undefined;
  onTower?: (name: string) => void;
  onPhone?: (id: number) => void;
}

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

/** Strato primary blue, used for the coverage shading. */
const COVERAGE_RGB: [number, number, number] = [71, 138, 206];

/** Downlink traffic: dots travel tower → phone, faster on faster connections. Cycles per second. */
const dotSpeed = (rate: number) => 0.15 + Math.min(0.8, Math.log10(1 + rate) * 0.35);

/** Signal strength shading, computed once per tower layout with the simulator's radio model. */
const Coverage = React.memo(({ towers, w, h }: { towers: Tower[]; w: number; h: number }) => {
  const href = useMemo(() => coverageImage(towers, w, h, S, COVERAGE_RGB), [towers, w, h]);
  return href ? <image href={href} x={PAD} y={PAD} width={h * S} height={w * S} preserveAspectRatio="none" /> : null;
});
Coverage.displayName = "Coverage";

function blend(s: Sample): Phone[] {
  if (s.alpha >= 1 || s.a === s.b) return s.b.phones;
  const before = new Map(s.a.phones.map((p) => [p.id, p]));
  return s.b.phones.map((p) => {
    const q = before.get(p.id);
    return q ? { ...p, x: lerp(q.x, p.x, s.alpha), y: lerp(q.y, p.y, s.alpha) } : p;
  });
}

/** Towers never move, so cache their layout by position and the coverage image is drawn only once. */
let lastLayout: { key: string; towers: Tower[] } | undefined;
function layoutOf(towers: Tower[]): Tower[] {
  const key = towers.map((t) => `${t.id}:${t.x}:${t.y}`).join("|");
  if (lastLayout?.key !== key) lastLayout = { key, towers };
  return lastLayout.towers;
}

/** Towers, their coverage, every phone and the links between them, replayed smoothly from the simulator's steps. */
export const CellMap = ({ sample, onTower, onPhone }: Props) => {
  const [s, setS] = useState<Sample | undefined>(() => sample(Date.now()));

  useEffect(() => {
    let raf = 0;
    const tick = () => {
      setS(sample(Date.now()));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [sample]);

  if (!s) return null;
  const frame = s.b;
  // Turned on its side: simulator y runs left to right, simulator x runs top to bottom.
  const px = (y: number) => PAD + y * S;
  const py = (x: number) => PAD + x * S;
  const W = frame.h * S + PAD * 2;
  const H = frame.w * S + PAD * 2;
  const phones = blend(s);
  const towers = new Map(frame.towers.map((t) => [t.id, t]));
  const now = Date.now() / 1000;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="cellmap" role="img" aria-label="Cell network map">
      <defs>
        <clipPath id="cm-area">
          <rect x={PAD} y={PAD} width={W - PAD * 2} height={H - PAD * 2} rx={10} />
        </clipPath>
      </defs>

      <rect x={PAD} y={PAD} width={W - PAD * 2} height={H - PAD * 2} rx={10} className="cm-area" />
      <g clipPath="url(#cm-area)">
        <Coverage towers={layoutOf(frame.towers)} w={frame.w} h={frame.h} />
        {frame.towers.map((t) => (
          <circle key={t.id} cx={px(t.y)} cy={py(t.x)} r={t.range * S} className="cm-coverage" />
        ))}
      </g>

      {phones.map((p) =>
        p.links.map((id) => {
          const t = towers.get(id);
          if (!t) return null;
          return <line key={`${p.id}-${id}`} x1={px(p.y)} y1={py(p.x)} x2={px(t.y)} y2={py(t.x)} className={`cm-link ${quality(p)}`} />;
        }),
      )}

      {phones.map((p) =>
        p.links.map((id) => {
          const t = towers.get(id);
          if (!t) return null;
          const speed = dotSpeed(p.rate);
          return [0].map((offset) => {
            const k = (now * speed + offset + p.id * 0.13) % 1;
            return <circle key={`${p.id}-${id}-${offset}`} cx={lerp(px(t.y), px(p.y), k)} cy={lerp(py(t.x), py(p.x), k)} r={3} className={`cm-packet ${quality(p)}`} />;
          });
        }),
      )}

      {frame.towers.map((t) => {
        const x = px(t.y);
        const y = py(t.x);
        return (
          <g
            key={t.id}
            className={`cm-tower${t.load ? "" : " idle"}`}
            transform={`translate(${x} ${y}) scale(1.25)`}
            onClick={() => onTower?.(t.name)}
          >
            <title>
              Tower {t.name} · {t.load} {t.load === 1 ? "phone" : "phones"}
            </title>
            <circle r={26} className="cm-hit" />
            <path d="M-11 -14 A 16 16 0 0 0 -11 4 M11 -14 A 16 16 0 0 1 11 4" className="cm-waves" />
            <path d="M-17 -19 A 24 24 0 0 0 -17 9 M17 -19 A 24 24 0 0 1 17 9" className="cm-waves outer" />
            <path d="M0 -6 L-8 18 M0 -6 L8 18 M-5 9 L5 9" className="cm-mast" />
            <circle cx={0} cy={-6} r={4.5} className="cm-mast-top" />
            <text x={0} y={34} className="cm-tower-name">
              {t.name}
            </text>
          </g>
        );
      })}

      {phones.map((p) => (
        <g
          key={p.id}
          className={`cm-phone ${p.state === "served" ? quality(p) : p.state.replace(" ", "-")}`}
          transform={`translate(${px(p.y)} ${py(p.x)})`}
          onClick={() => onPhone?.(p.id)}
        >
          <title>
            Phone {p.id} · {p.state}
          </title>
          {p.state === "dropped" && <circle r={14} className="cm-pulse" />}
          <circle r={14} className="cm-phone-dot" />
          <text y={0.5} className="cm-phone-id">
            {p.id}
          </text>
        </g>
      ))}
    </svg>
  );
};
