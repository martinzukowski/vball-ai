"use client";

import type { components } from "@/lib/api/schema";
import { formatDuration } from "@/lib/format";

type Rally = components["schemas"]["RallyOut"];

const btn =
  "rounded-md border border-slate-300 px-2.5 py-1.5 text-sm font-medium hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-900";

/** Trim/add/delete controls for the selected rally, using the video's current time. */
export function EditTools({
  selected,
  now,
  busy,
  reviewedAt,
  onTrim,
  onAdd,
  onDelete,
  onReview,
  onEditArea,
}: {
  selected: Rally | null;
  now: number;
  busy: boolean;
  reviewedAt: string | null | undefined;
  onTrim: (r: Rally, start: number, end: number) => void;
  onAdd: () => void;
  onDelete: (r: Rally) => void;
  onReview: () => void;
  onEditArea: () => void;
}) {
  const s = selected;
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-slate-200 bg-white shadow-sm p-3 dark:border-slate-800">
      {s ? (
        <>
          <div className="flex items-baseline justify-between text-sm">
            <span className="font-medium">
              Rally #{s.number} · {formatDuration(s.start_t)} – {formatDuration(s.end_t)}
              {s.edited && <span className="ml-2 text-xs text-amber-600">edited</span>}
            </span>
            <span className="text-xs text-slate-500 tabular-nums">now {formatDuration(now)}</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <button type="button" className={btn} disabled={busy || now >= s.end_t - 0.5}
              onClick={() => onTrim(s, now, s.end_t)}>Start ⟵ now</button>
            <button type="button" className={btn} disabled={busy || now <= s.start_t + 0.5}
              onClick={() => onTrim(s, s.start_t, now)}>End ⟵ now</button>
            <button type="button" className={btn} disabled={busy}
              onClick={() => onTrim(s, Math.max(0, s.start_t - 0.5), s.end_t)}>Start −½s</button>
            <button type="button" className={btn} disabled={busy}
              onClick={() => onTrim(s, s.start_t + 0.5, s.end_t)}>Start +½s</button>
            <button type="button" className={btn} disabled={busy}
              onClick={() => onTrim(s, s.start_t, s.end_t - 0.5)}>End −½s</button>
            <button type="button" className={btn} disabled={busy}
              onClick={() => onTrim(s, s.start_t, s.end_t + 0.5)}>End +½s</button>
            <button type="button" className={`${btn} text-red-700 dark:text-red-400`} disabled={busy}
              onClick={() => onDelete(s)}>Delete rally</button>
          </div>
        </>
      ) : (
        <p className="text-sm text-slate-500">Pick a rally from the list to trim or delete it.</p>
      )}
      <div className="flex flex-wrap items-center gap-1.5 border-t border-slate-200 pt-2 dark:border-slate-800">
        <button type="button" className={btn} disabled={busy} onClick={onAdd}>
          + Add rally at {formatDuration(now)}
        </button>
        <button type="button" className={btn} disabled={busy} onClick={onEditArea}>
          Mark play area
        </button>
        <button type="button" className={`${btn} ml-auto`} disabled={busy} onClick={onReview}>
          ✓ Mark reviewed
        </button>
      </div>
      <p className="text-xs text-slate-500">
        {reviewedAt
          ? `Reviewed ${new Date(reviewedAt).toLocaleString()}. Mark again after more fixes.`
          : "When the rallies look right, mark the video reviewed: it becomes training data for the rally model."}
      </p>
    </div>
  );
}
