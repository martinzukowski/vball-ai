"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { apiErrorMessage, authedApi } from "@/lib/api/authed";
import type { components } from "@/lib/api/schema";
import { formatDuration } from "@/lib/format";

type Video = components["schemas"]["VideoOut"];
type Loaded = { video: Video; src: string | null; srcError: string | null };

// Playback page. The rally timeline and editing land here in M5/M6.
export function VideoPlayer() {
  const { id } = useParams<{ id: string }>();
  const [state, setState] = useState<Loaded | { error: string } | null>(null);

  useEffect(() => {
    let stale = false;
    (async () => {
      const api = authedApi();
      const params = { params: { path: { video_id: id } } };
      const v = await api.GET("/videos/{video_id}", params);
      if (stale) return;
      if (!v.data) {
        setState({ error: apiErrorMessage(v.error, v.response.status) });
        return;
      }
      const p = await api.GET("/videos/{video_id}/playback", params);
      if (stale) return;
      setState({
        video: v.data,
        src: p.data?.url ?? null,
        srcError: p.data ? null : apiErrorMessage(p.error, p.response.status),
      });
    })();
    return () => {
      stale = true;
    };
  }, [id]);

  if (state === null) return <p className="text-zinc-500">Loading…</p>;
  if ("error" in state) {
    return (
      <p role="alert" className="text-red-700 dark:text-red-400">
        {state.error}. <Link href="/videos" className="underline">Back to videos</Link>
      </p>
    );
  }
  const { video, src, srcError } = state;
  return (
    <>
      <div className="flex items-baseline justify-between gap-3">
        <h1 className="truncate text-xl font-semibold tracking-tight">{video.filename}</h1>
        <Link href="/videos" className="shrink-0 text-sm text-zinc-500 hover:underline">
          All videos
        </Link>
      </div>
      {src ? (
        <video
          src={src}
          controls
          playsInline
          preload="metadata"
          poster={video.poster_url ?? undefined}
          className="aspect-video w-full rounded-lg bg-black"
        />
      ) : (
        <p role="alert" className="text-red-700 dark:text-red-400">
          {srcError ?? "This video can't be played yet."}
        </p>
      )}
      <p className="text-sm text-zinc-500">
        {video.duration_s != null && formatDuration(video.duration_s)}
        {video.width && video.height && ` · ${video.width}×${video.height} original`}
        {video.status !== "ready" && ` · ${video.status}`}
      </p>
    </>
  );
}
