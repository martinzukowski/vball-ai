"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { apiErrorMessage, authedApi } from "@/lib/api/authed";
import type { components } from "@/lib/api/schema";
import { formatBytes, formatDate, formatDuration } from "@/lib/format";

import { Uploader } from "./uploader";

type Video = components["schemas"]["VideoOut"];
type Usage = components["schemas"]["StorageUsage"];

const TONE = {
  amber: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  sky: "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200",
  green: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  red: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200",
  grey: "bg-zinc-100 text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300",
};

const STATUS_LABEL: Record<Video["status"], { text: string; tone: string }> = {
  uploading: { text: "Upload unfinished", tone: TONE.amber },
  uploaded: { text: "Queued", tone: TONE.sky },
  ingesting: { text: "Preparing", tone: TONE.sky },
  ready: { text: "Ready", tone: TONE.green },
  failed: { text: "Failed", tone: TONE.red },
  deleted: { text: "Deleted", tone: TONE.grey },
};

const STAGE_LABEL: Record<string, string> = {
  download: "Fetching upload",
  probe: "Checking video",
  transcode: "Converting",
  poster: "Making thumbnail",
  upload: "Saving",
};

const POLL_MS = 4000;

function isProcessing(v: Video): boolean {
  return v.status === "uploaded" || v.status === "ingesting";
}

export function VideoLibrary() {
  const [videos, setVideos] = useState<Video[] | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const reload = () => setReloadKey((k) => k + 1);

  useEffect(() => {
    let stale = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    authedApi()
      .GET("/videos")
      .then(({ data, error: err, response }) => {
        if (stale) return;
        if (!data) {
          setError(apiErrorMessage(err, response.status));
          return;
        }
        setError(null);
        setVideos(data.videos);
        setUsage(data.storage);
        // Processing happens on the server; refresh while anything is in progress.
        if (data.videos.some(isProcessing)) {
          timer = setTimeout(() => setReloadKey((k) => k + 1), POLL_MS);
        }
      });
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [reloadKey]);

  async function remove(video: Video) {
    if (!confirm(`Delete "${video.filename}"? This removes the video and its analysis.`)) return;
    const { error: err, response } = await authedApi().DELETE("/videos/{video_id}", {
      params: { path: { video_id: video.id } },
    });
    if (!response.ok) setError(apiErrorMessage(err, response.status));
    reload();
  }

  async function retry(video: Video) {
    const { error: err, response } = await authedApi().POST("/videos/{video_id}/retry", {
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
            <VideoRow key={v.id} video={v} onDelete={() => remove(v)} onRetry={() => retry(v)} />
          ))}
        </ul>
      )}
    </div>
  );
}

function VideoRow({
  video: v,
  onDelete,
  onRetry,
}: {
  video: Video;
  onDelete: () => void;
  onRetry: () => void;
}) {
  const ready = v.status === "ready";
  const job = v.processing;
  const pct = job ? Math.round(job.progress * 100) : 0;
  const thumb = (
    <div className="relative aspect-video w-24 shrink-0 overflow-hidden rounded-md bg-zinc-200 dark:bg-zinc-800">
      {v.poster_url && (
        // Presigned, short-lived URL from our storage; Next's image optimizer can't fetch it.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={v.poster_url} alt="" className="h-full w-full object-cover" />
      )}
    </div>
  );
  return (
    <li className="flex items-center gap-3 py-3">
      {ready ? (
        <Link href={`/videos/${v.id}`} aria-label={`Open ${v.filename}`}>
          {thumb}
        </Link>
      ) : (
        thumb
      )}
      <div className="min-w-0 flex-1">
        {ready ? (
          <Link href={`/videos/${v.id}`} className="block truncate font-medium hover:underline">
            {v.filename}
          </Link>
        ) : (
          <p className="truncate font-medium">{v.filename}</p>
        )}
        <p className="text-xs text-zinc-500">
          {formatDate(v.created_at)} · {formatBytes(v.size_bytes)}
          {v.duration_s != null && ` · ${formatDuration(v.duration_s)}`}
        </p>
        {isProcessing(v) && (
          <div className="mt-1 flex items-center gap-2">
            <div className="h-1.5 w-28 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800">
              <div className="h-full bg-sky-600 transition-[width]" style={{ width: `${pct}%` }} />
            </div>
            <span className="text-xs text-zinc-500 tabular-nums">
              {job?.state === "running"
                ? `${STAGE_LABEL[job.stage ?? ""] ?? "Working"} · ${pct}%`
                : job?.error_code === "transient"
                  ? "Retrying soon"
                  : "Waiting for a worker"}
            </span>
          </div>
        )}
        {v.status === "failed" && v.error_message && (
          <p className="text-xs text-red-700 dark:text-red-400">{v.error_message}</p>
        )}
      </div>
      <span
        className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_LABEL[v.status].tone}`}
      >
        {STATUS_LABEL[v.status].text}
      </span>
      {v.status === "failed" && v.original_available && (
        <button
          type="button"
          onClick={onRetry}
          className="shrink-0 rounded-md px-2 py-1 text-sm font-medium hover:bg-zinc-100 dark:hover:bg-zinc-900"
        >
          Retry
        </button>
      )}
      <button
        type="button"
        onClick={onDelete}
        aria-label={`Delete ${v.filename}`}
        className="shrink-0 rounded-md px-2 py-1 text-sm text-zinc-500 hover:bg-zinc-100 hover:text-red-700 dark:hover:bg-zinc-900"
      >
        Delete
      </button>
    </li>
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
