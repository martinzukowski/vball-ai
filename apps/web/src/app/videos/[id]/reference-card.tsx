"use client";

import type { components } from "@/lib/api/schema";
import { formatDuration } from "@/lib/format";

type Reference = components["schemas"]["ReferenceCheck"];

const NAMES: Record<string, string> = { balltime: "Balltime" };

/** How our rallies compare with an imported reference (e.g. Balltime) for this video:
 *  a rally counts as matching when the two overlap by at least half. */
export function ReferenceCard({
  reference: ref,
  onSeek,
}: {
  reference: Reference;
  onSeek: (t: number) => void;
}) {
  const name = NAMES[ref.source] ?? ref.source;
  const pct = Math.round(ref.f1 * 100);
  // When the model learned from this very match, its agreement here is optimistic; lead with
  // the score it got when this gym (or match) was left out of training instead.
  const held = ref.in_training && ref.held_out_f1 != null ? Math.round(ref.held_out_f1 * 100) : null;
  const shown = held ?? pct;
  const tone =
    shown >= 95
      ? "text-emerald-600"
      : shown >= 80
        ? "text-blue-600"
        : shown >= 60
          ? "text-amber-600"
          : "text-red-600";
  const extra = ref.n_ours - ref.matched;
  return (
    <section className="mt-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-4">
        <div className={`text-3xl font-bold tabular-nums ${tone}`}>{shown}%</div>
        <div className="text-sm">
          <p className="font-semibold">
            {held != null
              ? `Agreement with ${name} on unseen footage`
              : `Agreement with ${name}${ref.in_training ? " (model trained on this match)" : ""}`}
          </p>
          {held != null && (
            <p className="text-slate-500">
              The model was trained on this match. Trained without this{" "}
              {ref.held_out_test === "gym" ? "gym" : "match"}, it scored {held}%; today&apos;s cut
              agrees {pct}%.
            </p>
          )}
          <p className="text-slate-500">
            {ref.matched} of {ref.n_reference} {name} rallies matched
            {extra > 0 && ` · ${extra} extra on our side`}
            {ref.missed.length > 0 && ` · ${ref.missed.length} missed`}
          </p>
        </div>
      </div>
      {ref.missed.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-slate-500">Missed (tap to watch):</span>
          {ref.missed.map(([s, e]) => (
            <button
              key={s}
              type="button"
              onClick={() => onSeek(s)}
              className="rounded-md border border-slate-200 px-2 py-1 tabular-nums hover:bg-slate-50"
            >
              {formatDuration(s)}–{formatDuration(e)}
            </button>
          ))}
        </div>
      )}
      <p className="mt-2 text-xs text-slate-400">
        A rally counts as matching when it overlaps a {name} rally by at least half.
      </p>
    </section>
  );
}
