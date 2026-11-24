"use client";

import { useEffect, useMemo, useState } from "react";

import { apiErrorMessage, createAuthedApi } from "@/lib/api/authed";
import type { components } from "@/lib/api/schema";
import { formatBytes, formatDate, formatDuration } from "@/lib/format";

import { Uploader } from "./uploader";

type Video = components["schemas"]["VideoOut"];
type Usage = components["schemas"]["StorageUsage"];

const STATUS_LABEL: Record<Video["status"], { text: string; tone: string }> = {
  uploading: { text: "Upload unfinished", tone: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200" },
  uploaded: { text: "Uploaded", tone: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200" },
  ingesting: { text: "Preparing", tone: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200" },
  ready: { text: "Ready", tone: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200" },
  failed: { text: "Failed", tone: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200" },
  deleted: { text: "Deleted", tone: "bg-zinc-100 text-zinc-700" },
};

export function VideoLibrary() {
  const api = useMemo(() => createAuthedApi(), []);
  const [videos, setVideos] = useState<Video[] | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const reload = () => setReloadKey((k) => k + 1);

  useEffect(() => {
    let stale = false;
    api.GET("/videos").then(({ data, error: err, response }) => {
      if (stale) return;
      if (!data) {
        setError(apiErrorMessage(err, response.status));
        return;
      }
      setError(null);
      setVideos(data.videos);
      setUsage(data.storage);
    });
    return () => {
      stale = true;
    };
  }, [api, reloadKey]);

  async function remove(video: Video) {
    if (!confirm(`Delete "${video.filename}"? This removes the video and its analysis.`)) return;
    const { error: err, response } = await api.DELETE("/videos/{video_id}", {
      params: { path: { video_id: video.id } },
    });
    if (!response.ok) setError(apiErrorMessage(err, response.status));
    reload();
  }

  return (
    <div className="flex flex-col gap-6">
      <Uploader onUploaded={reload} />
      {usage && <StorageBar usage={usage} />}
      {error && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {error}
        </p>
      )}
      {videos === null && !error && <p className="text-zinc-500">Loading videos…</p>}
      {videos?.length === 0 && (
        <p className="text-zinc-500">No videos yet. Upload a match to get started.</p>
      )}
      {videos && videos.length > 0 && (
        <ul className="flex flex-col divide-y divide-zinc-200 dark:divide-zinc-800">
          {videos.map((v) => (
            <li key={v.id} className="flex items-center gap-3 py-3">
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium">{v.filename}</p>
                <p className="text-xs text-zinc-500">
                  {formatDate(v.created_at)} · {formatBytes(v.size_bytes)}
                  {v.duration_s != null && ` · ${formatDuration(v.duration_s)}`}
                </p>
                {v.error_message && (
                  <p className="text-xs text-red-700 dark:text-red-400">{v.error_message}</p>
                )}
              </div>
              <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_LABEL[v.status].tone}`}>
                {STATUS_LABEL[v.status].text}
              </span>
              <button
                type="button"
                onClick={() => remove(v)}
                aria-label={`Delete ${v.filename}`}
                className="shrink-0 rounded-md px-2 py-1 text-sm text-zinc-500 hover:bg-zinc-100 hover:text-red-700 dark:hover:bg-zinc-900"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function StorageBar({ usage }: { usage: Usage }) {
  const pct = Math.min(100, Math.round((usage.used_bytes / usage.cap_bytes) * 100));
  return (
    <div className="flex flex-col gap-1 text-xs text-zinc-500">
      <div className="flex justify-between">
        <span>Storage</span>
        <span className="tabular-nums">
          {formatBytes(usage.used_bytes)} of {formatBytes(usage.cap_bytes)} free-tier limit
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
        <div
          className={`h-full ${pct > 85 ? "bg-red-600" : "bg-zinc-500"}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
