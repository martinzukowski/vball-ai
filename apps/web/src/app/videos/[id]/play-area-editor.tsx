"use client";

import { useEffect, useRef, useState, type PointerEvent } from "react";

type Pt = [number, number];

const DEFAULT: Pt[] = [
  [0.15, 0.3],
  [0.85, 0.3],
  [0.95, 0.85],
  [0.05, 0.85],
];

/**
 * Drag the corners of a polygon around the court (and the space above it where players jump)
 * on top of the paused video. Coordinates are normalised to the picture, so letterboxing is
 * accounted for using the video's aspect ratio.
 */
export function PlayAreaEditor({
  initial,
  aspect,
  onSave,
  onCancel,
}: {
  initial: number[][] | null | undefined;
  aspect: number;
  onSave: (points: number[][]) => void;
  onCancel: () => void;
}) {
  const [pts, setPts] = useState<Pt[]>(
    initial && initial.length >= 3 ? initial.map((p) => [p[0], p[1]] as Pt) : DEFAULT,
  );
  const [drag, setDrag] = useState<number | null>(null);
  const box = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 16, h: 9 });

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) =>
      setSize({ w: entry.contentRect.width, h: entry.contentRect.height }),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Content rect of an object-contain video inside the element.
  function toNorm(e: PointerEvent): Pt {
    const r = box.current!.getBoundingClientRect();
    let w = r.width;
    let h = r.height;
    let ox = 0;
    let oy = 0;
    if (r.width / r.height > aspect) {
      w = r.height * aspect;
      ox = (r.width - w) / 2;
    } else {
      h = r.width / aspect;
      oy = (r.height - h) / 2;
    }
    const clamp = (v: number) => Math.min(1.1, Math.max(-0.1, v));
    return [clamp((e.clientX - r.left - ox) / w), clamp((e.clientY - r.top - oy) / h)];
  }

  // Map normalised points to the SVG's 0..100 viewBox, honouring letterboxing.
  function toView([x, y]: Pt): Pt {
    if (size.w / size.h > aspect) {
      const w = (size.h * aspect) / size.w;
      return [(0.5 - w / 2 + x * w) * 100, y * 100];
    }
    const h = size.w / aspect / size.h;
    return [x * 100, (0.5 - h / 2 + y * h) * 100];
  }

  const view = pts.map(toView);
  return (
    <div className="absolute inset-0 z-10">
      <svg
        ref={box}
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        className="h-full w-full touch-none"
        onPointerMove={(e) => {
          if (drag === null) return;
          const p = toNorm(e);
          setPts((prev) => prev.map((q, i) => (i === drag ? p : q)));
        }}
        onPointerUp={() => setDrag(null)}
      >
        <polygon
          points={view.map((p) => p.join(",")).join(" ")}
          fill="rgba(250,204,21,0.15)"
          stroke="rgb(250,204,21)"
          strokeWidth={0.4}
          vectorEffect="non-scaling-stroke"
        />
        {view.map(([x, y], i) => (
          <ellipse
            key={i}
            cx={x}
            cy={y}
            rx={1.6}
            ry={2.8}
            fill="rgb(250,204,21)"
            stroke="black"
            strokeWidth={0.3}
            className="cursor-grab"
            onPointerDown={(e) => {
              (e.target as Element).setPointerCapture(e.pointerId);
              setDrag(i);
            }}
          />
        ))}
      </svg>
      <div className="absolute inset-x-2 bottom-2 flex flex-wrap items-center gap-2 rounded-md bg-black/70 p-2 text-xs text-white">
        <span className="flex-1">
          Drag the yellow corners around the court and the space above it. People outside the
          area (bench, spectators, passers-by) are ignored.
        </span>
        <button type="button" className="rounded bg-white/20 px-2 py-1" onClick={() => setPts(DEFAULT)}>
          Reset
        </button>
        <button type="button" className="rounded bg-white/20 px-2 py-1" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="rounded bg-yellow-400 px-2 py-1 font-semibold text-black"
          onClick={() => onSave(pts.map(([x, y]) => [x, y]))}
        >
          Save & re-detect
        </button>
      </div>
    </div>
  );
}
