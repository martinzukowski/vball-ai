"use client";

import { useEffect, useState } from "react";

import { apiErrorMessage, authedApi } from "@/lib/api/authed";
import type { components } from "@/lib/api/schema";
import { formatBytes, formatDuration } from "@/lib/format";

type Export = components["schemas"]["ExportOut"];

const btn =
  "rounded-md border border-slate-300 px-2.5 py-1.5 text-sm font-medium hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-900";

/** Request highlight videos (cut on the server) and download finished ones. */
export function HighlightsPanel({
  videoId,
  allIds,
  starredIds,
  baseName,
}: {
  videoId: string;
  allIds: string[];
  starredIds: string[];
  baseName: string;
}) {
  const [exports, setExports] = useState<Export[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let stale = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    authedApi()
      .GET("/videos/{video_id}/highlights", { params: { path: { video_id: videoId } } })
      .then(({ data }) => {
        if (stale || !data) return;
        setExports(data);
        if (data.some((e) => e.status === "pending")) {
          timer = setTimeout(() => setReloadKey((k) => k + 1), 3000);
        }
      });
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [videoId, reloadKey]);

  async function create(ids: string[], name: string) {
    setError(null);
    const res = await authedApi().POST("/videos/{video_id}/highlights", {
      params: { path: { video_id: videoId } },
      body: { rally_ids: ids, name, padding_s: 0.5 },
    });
    if (!res.data) setError(apiErrorMessage(res.error, res.response.status));
    setReloadKey((k) => k + 1);
  }

  async function remove(ex: Export) {
    await authedApi().DELETE("/highlights/{export_id}", {
      params: { path: { export_id: ex.id } },
    });
    setReloadKey((k) => k + 1);
  }

  const short = baseName.replace(/\.[^.]+$/, "").slice(0, 60);
  return (
    <section className="flex flex-col gap-2 rounded-xl border border-slate-200 bg-white shadow-sm p-3 dark:border-slate-800">
      <h2 className="text-sm font-semibold">Highlight videos</h2>
      <div className="flex flex-wrap gap-1.5">
        <button type="button" className={btn} disabled={allIds.length === 0}
          onClick={() => create(allIds, `${short} - all rallies`)}>
          Export all rallies ({allIds.length})
        </button>
        <button type="button" className={btn} disabled={starredIds.length === 0}
          onClick={() => create(starredIds, `${short} - starred`)}>
          Export starred ({starredIds.length})
        </button>
      </div>
      {error && <p role="alert" className="text-sm text-red-700 dark:text-red-400">{error}</p>}
      {exports && exports.length > 0 && (
        <ul className="flex flex-col divide-y divide-slate-100 text-sm dark:divide-slate-900">
          {exports.map((ex) => (
            <li key={ex.id} className="flex items-center gap-2 py-2">
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium">{ex.name}</p>
                <p className="text-xs text-slate-500">
                  {ex.clip_count} clips · {formatDuration(ex.duration_s)}
                  {ex.size_bytes ? ` · ${formatBytes(ex.size_bytes)}` : ""}
                  {ex.status === "pending" && ` · making it… ${Math.round(ex.progress * 100)}%`}
                  {ex.status === "failed" && ` · failed: ${ex.error_message ?? "unknown error"}`}
                </p>
              </div>
              {ex.download_url && (
                <a href={ex.download_url} className={btn}>
                  Download
                </a>
              )}
              <button type="button" className="px-2 py-1 text-xs text-slate-500 hover:text-red-700"
                onClick={() => remove(ex)} aria-label={`Delete ${ex.name}`}>
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-slate-500">
        Made from the 720p playback copy, with ½ s of padding around each rally. Kept for 30 days.
      </p>
    </section>
  );
}
