import type { Metadata } from "next";
import { Suspense } from "react";

import { VideoPlayer } from "./video-player";

export const metadata: Metadata = { title: "Video · VBall" };

export default function VideoPage() {
  return (
    <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col gap-4 px-4 py-6">
      {/* Reads the route parameter and loads on the client with the user's token. */}
      <Suspense fallback={<p className="text-slate-500">Loading…</p>}>
        <VideoPlayer />
      </Suspense>
    </main>
  );
}
