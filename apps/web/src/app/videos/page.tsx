import type { Metadata } from "next";

import { VideoLibrary } from "./video-library";

export const metadata: Metadata = { title: "Videos · VBall" };

// Signed-out visitors are redirected to /login by src/proxy.ts. The library itself loads on
// the client with the user's token.
export default function VideosPage() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-6 px-4 py-8">
      <h1 className="text-2xl font-semibold tracking-tight">Videos</h1>
      <VideoLibrary />
    </main>
  );
}
