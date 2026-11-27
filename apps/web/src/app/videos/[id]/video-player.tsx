"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiErrorMessage, authedApi } from "@/lib/api/authed";
import type { components } from "@/lib/api/schema";
import { formatDuration } from "@/lib/format";

import { EditTools } from "./edit-tools";
import { HighlightsPanel } from "./highlights-panel";
import { PlayAreaEditor } from "./play-area-editor";

type Video = components["schemas"]["VideoOut"];
type Rallies = components["schemas"]["RalliesOut"];
type Rally = components["schemas"]["RallyOut"];
type Tab = "all" | "longest" | "starred";

const POLL_MS = 4000;

function describeDetector(detector: string): string {
  if (detector.startsWith("learned:")) {
    return `Detected by the learned rally model (version ${detector.slice(8)}).`;
  }
  if (detector.startsWith("reference:")) {
    return `Rallies imported from ${detector.slice(10)} as a reference.`;
  }
  return `Detected by ${detector} (rules-based; motion and whistle cues).`;
}

function clock(s: number): string {
  return formatDuration(s);
}

// Starred rallies are remembered in this browser only for now (synced stars come with edits).
function useStars(videoId: string) {
  const key = `vball:stars:${videoId}`;
  // Read once on mount. Safe for hydration: stars only render after rallies load in the browser.
  const [stars, setStars] = useState<Set<string>>(() => {
    if (typeof window === "undefined") return new Set();
    try {
      return new Set(JSON.parse(localStorage.getItem(key) ?? "[]") as string[]);
    } catch {
      return new Set();
    }
  });
  const toggle = (id: string) =>
    setStars((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      try {
        localStorage.setItem(key, JSON.stringify([...next]));
      } catch {}
      return next;
    });
  return { stars, toggle };
}

export function VideoPlayer() {
  const { id } = useParams<{ id: string }>();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [video, setVideo] = useState<Video | null>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<Rallies | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [tab, setTab] = useState<Tab>("all");
  const [ralliesOnly, setRalliesOnly] = useState(true);
  const [now, setNow] = useState(0);
  const { stars, toggle } = useStars(id);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [areaMode, setAreaMode] = useState(false);
  const [editBusy, setEditBusy] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    (async () => {
      const api = authedApi();
      const params = { params: { path: { video_id: id } } };
      const v = await api.GET("/videos/{video_id}", params);
      if (stale) return;
      if (!v.data) return setError(apiErrorMessage(v.error, v.response.status));
      setVideo(v.data);
      const p = await api.GET("/videos/{video_id}/playback", params);
      if (!stale) setSrc(p.data?.url ?? null);
    })();
    return () => {
      stale = true;
    };
  }, [id]);

  useEffect(() => {
    let stale = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    authedApi()
      .GET("/videos/{video_id}/rallies", { params: { path: { video_id: id } } })
      .then(({ data: d }) => {
        if (stale || !d) return;
        setData(d);
        const st = d.job?.state;
        if (st === "queued" || st === "running" || (!d.analysis && !d.job)) {
          timer = setTimeout(() => setReloadKey((k) => k + 1), POLL_MS);
        }
      });
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [id, reloadKey]);

  const rallies = useMemo(() => data?.rallies ?? [], [data]);
  const current = rallies.find((r) => now >= r.start_t && now < r.end_t) ?? null;

  // "Rallies only" playback: jump over dead time.
  const onTime = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    const t = v.currentTime;
    setNow(t);
    if (!ralliesOnly || v.paused || rallies.length === 0) return;
    if (!rallies.some((r) => t >= r.start_t && t < r.end_t)) {
      const next = rallies.find((r) => r.start_t > t);
      if (next) v.currentTime = next.start_t;
      else v.pause();
    }
  }, [ralliesOnly, rallies]);

  function play(r: Rally) {
    setSelectedId(r.id);
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = r.start_t;
    void v.play();
  }

  async function edit(body: {
    op: "add" | "modify" | "delete";
    rally_id?: string;
    start_t?: number;
    end_t?: number;
  }) {
    setEditBusy(true);
    setEditError(null);
    const res = await authedApi().POST("/videos/{video_id}/rallies/edits", {
      params: { path: { video_id: id } },
      body,
    });
    setEditBusy(false);
    if (res.data) setData(res.data);
    else setEditError(apiErrorMessage(res.error, res.response.status));
    return res.data;
  }

  async function addAtNow() {
    const t = videoRef.current?.currentTime ?? now;
    const start = Math.max(0, t - 1);
    const end = Math.min(video?.duration_s ?? t + 8, t + 7);
    const d = await edit({ op: "add", start_t: start, end_t: end });
    const added = d?.rallies.find((r) => Math.abs(r.start_t - start) < 0.05);
    if (added) setSelectedId(added.id);
  }

  async function markReviewed() {
    setEditBusy(true);
    setEditError(null);
    const res = await authedApi().POST("/videos/{video_id}/review", {
      params: { path: { video_id: id } },
    });
    setEditBusy(false);
    if (res.data) setData(res.data);
    else setEditError(apiErrorMessage(res.error, res.response.status));
  }

  async function savePlayArea(points: number[][]) {
    setEditBusy(true);
    const res = await authedApi().PUT("/videos/{video_id}/play-area", {
      params: { path: { video_id: id } },
      body: { points },
    });
    setEditBusy(false);
    if (res.data) {
      setData(res.data);
      setAreaMode(false);
      setReloadKey((k) => k + 1);
    } else setEditError(apiErrorMessage(res.error, res.response.status));
  }

  async function reanalyze(sensitivity: number) {
    const { data: d } = await authedApi().POST("/videos/{video_id}/analyze", {
      params: { path: { video_id: id } },
      body: { sensitivity },
    });
    if (d) setData(d);
    setReloadKey((k) => k + 1);
  }

  if (error) {
    return (
      <p role="alert" className="text-red-700 dark:text-red-400">
        {error}. <Link href="/videos" className="underline">Back to videos</Link>
      </p>
    );
  }
  if (!video) return <p className="text-slate-500">Loading…</p>;

  const shown =
    tab === "longest"
      ? [...rallies].sort((a, b) => b.end_t - b.start_t - (a.end_t - a.start_t))
      : tab === "starred"
        ? rallies.filter((r) => stars.has(r.id))
        : rallies;
  const selected = rallies.find((r) => r.id === selectedId) ?? current;
  const kept = rallies.reduce((s, r) => s + r.end_t - r.start_t, 0);
  const sens = data?.analysis?.sensitivity ?? 0;
  const busy = data?.job?.state === "queued" || data?.job?.state === "running";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-slate-200 pb-3 dark:border-slate-800">
        <h1 className="min-w-0 flex-1 truncate text-lg font-semibold">{video.filename}</h1>
        {rallies.length > 0 && (
          <span className="text-sm text-slate-500 tabular-nums">
            {rallies.length} rallies · {clock(kept)} of{" "}
            {video.duration_s ? clock(video.duration_s) : "?"}
          </span>
        )}
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={ralliesOnly}
            onChange={(e) => setRalliesOnly(e.target.checked)}
          />
          Skip dead time
        </label>
        <Link href="/videos" className="text-sm text-slate-500 hover:underline">
          All videos
        </Link>
      </div>

      <div className="flex flex-col gap-4 lg:flex-row">
        <div className="lg:min-w-0 lg:flex-[2]">
          <div className="relative">
          {src ? (
            <video
              ref={videoRef}
              src={src}
              controls
              playsInline
              preload="metadata"
              poster={video.poster_url ?? undefined}
              onTimeUpdate={onTime}
              className="aspect-video w-full rounded-lg bg-black object-contain"
            />
          ) : (
            <div className="flex aspect-video w-full items-center justify-center rounded-lg bg-slate-900 text-slate-400">
              {video.status === "ready" ? "Loading video…" : "Still preparing this video…"}
            </div>
          )}
          {areaMode && (
            <PlayAreaEditor
              initial={data?.play_area}
              aspect={video.width && video.height ? video.width / video.height : 16 / 9}
              onSave={savePlayArea}
              onCancel={() => setAreaMode(false)}
            />
          )}
          </div>
          <div className="mt-3">
            <EditTools
              selected={selected}
              now={now}
              busy={editBusy || busy}
              reviewedAt={data?.reviewed_at}
              onTrim={(r, start, end) => edit({ op: "modify", rally_id: r.id, start_t: start, end_t: end })}
              onAdd={addAtNow}
              onDelete={(r) => {
                if (confirm(`Delete rally #${r.number}?`)) void edit({ op: "delete", rally_id: r.id });
              }}
              onReview={markReviewed}
              onEditArea={() => {
                videoRef.current?.pause();
                setRalliesOnly(false);
                setAreaMode(true);
              }}
            />
            <div className="mt-3">
              <HighlightsPanel
                videoId={id}
                allIds={rallies.map((r) => r.id)}
                starredIds={rallies.filter((r) => stars.has(r.id)).map((r) => r.id)}
                baseName={video.filename}
              />
            </div>
            {editError && (
              <p role="alert" className="mt-2 text-sm text-red-700 dark:text-red-400">
                {editError}
              </p>
            )}
          </div>
          {data?.analysis && (
            <p className="mt-2 text-xs text-slate-500">
              {describeDetector(data.analysis.detector)}
              {!data.analysis.used_play_area &&
                " No play area marked: motion is measured on the whole frame, so people walking past the camera can cause mistakes."}
              {data.analysis.camera_moves.length > 0 &&
                ` Camera moved ${data.analysis.camera_moves.length}× (at ${data.analysis.camera_moves.map(clock).join(", ")}).`}
            </p>
          )}
        </div>

        <aside className="flex max-h-[75vh] flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm lg:flex-1 dark:border-slate-800">
          <div className="flex border-b border-slate-200 text-sm dark:border-slate-800">
            {(
              [
                ["all", "Rallies"],
                ["longest", "Longest"],
                ["starred", `Starred${stars.size ? ` (${stars.size})` : ""}`],
              ] as [Tab, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                className={`flex-1 px-3 py-2.5 font-medium ${
                  tab === key
                    ? "border-b-2 border-blue-600 text-slate-950 dark:text-white"
                    : "text-slate-500"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="flex items-center justify-between gap-2 border-b border-slate-200 px-3 py-2 text-xs text-slate-500 dark:border-slate-800">
            <span>{busy ? "Detecting rallies…" : "Sensitivity"}</span>
            <div className="flex items-center gap-1">
              <button
                type="button"
                disabled={busy || sens <= -5}
                onClick={() => reanalyze(sens - 2)}
                className="rounded-md border border-slate-300 px-2 py-1 disabled:opacity-40 dark:border-slate-700"
              >
                Fewer
              </button>
              <span className="w-6 text-center tabular-nums">{sens}</span>
              <button
                type="button"
                disabled={busy || sens >= 5}
                onClick={() => reanalyze(sens + 2)}
                className="rounded-md border border-slate-300 px-2 py-1 disabled:opacity-40 dark:border-slate-700"
              >
                More
              </button>
            </div>
          </div>
          <ul className="flex-1 overflow-y-auto">
            {!data && <li className="p-4 text-sm text-slate-500">Loading rallies…</li>}
            {data && !data.analysis && (
              <li className="p-4 text-sm text-slate-500">
                {data.job?.state === "failed"
                  ? `Rally detection failed: ${data.job.error_message ?? "unknown error"}`
                  : `Finding rallies… ${data.job ? Math.round(data.job.progress * 100) : 0}%`}
              </li>
            )}
            {data?.analysis && shown.length === 0 && (
              <li className="p-4 text-sm text-slate-500">
                {tab === "starred" ? "No starred rallies yet." : "No rallies detected."}
              </li>
            )}
            {shown.map((r) => {
              const active = selected?.id === r.id;
              return (
                <li
                  key={r.id}
                  className={`flex items-center gap-2 border-b border-slate-100 px-3 dark:border-slate-900 ${
                    active ? "bg-blue-600 text-white" : ""
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => play(r)}
                    className="flex min-h-12 flex-1 items-center gap-3 text-left text-sm"
                  >
                    <span className="tabular-nums">
                      {clock(r.start_t)} – {clock(r.end_t)}
                    </span>
                    <span className={active ? "text-white/60" : "text-slate-300 dark:text-slate-700"}>
                      |
                    </span>
                    <span className="font-medium">Rally #{r.number}</span>
                    <span className={`ml-auto text-xs tabular-nums ${active ? "text-white/80" : "text-slate-500"}`}>
                      {Math.round(r.end_t - r.start_t)}s
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => toggle(r.id)}
                    aria-label={stars.has(r.id) ? `Unstar rally ${r.number}` : `Star rally ${r.number}`}
                    className="p-2 text-lg leading-none"
                  >
                    {stars.has(r.id) ? "★" : "☆"}
                  </button>
                </li>
              );
            })}
          </ul>
        </aside>
      </div>
    </div>
  );
}
