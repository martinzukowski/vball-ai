"use client";

import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ChangeEvent,
} from "react";

import { authedApi } from "@/lib/api/authed";
import { formatBytes } from "@/lib/format";
import { browserPort, localPendingStore, pendingUploads } from "@/lib/upload/browser-port";
import { uploadFile, UploadAbortedError, type Progress } from "@/lib/upload/engine";

type State =
  | { kind: "idle" }
  | { kind: "uploading"; name: string; progress: Progress; bytesPerSecond: number }
  | { kind: "done"; name: string }
  | { kind: "error"; name: string; message: string };

export function Uploader({ onUploaded }: { onUploaded: () => void }) {
  const [state, setState] = useState<State>({ kind: "idle" });
  const abortRef = useRef<AbortController | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // Unfinished uploads live in localStorage; read on each render (cheap), empty on the server.
  const pendingText = useSyncExternalStore(
    noSubscribe,
    () => pendingUploads().map((p) => p.name).join(SEP),
    () => "",
  );
  const pending = pendingText ? pendingText.split(SEP) : [];

  // Warn before leaving mid-upload (progress is kept and can be resumed, but it's slower).
  useEffect(() => {
    if (state.kind !== "uploading") return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    addEventListener("beforeunload", warn);
    return () => removeEventListener("beforeunload", warn);
  }, [state.kind]);

  async function start(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const wakeLock = await requestWakeLock();
    const startedAt = Date.now();
    let startBytes = -1;
    setState({
      kind: "uploading",
      name: file.name,
      bytesPerSecond: 0,
      progress: { sentBytes: 0, totalBytes: file.size, partsDone: 0, partCount: 0 },
    });
    try {
      await uploadFile(file, browserPort(authedApi()), localPendingStore, {
        signal: ctrl.signal,
        onProgress: (progress) => {
          if (startBytes < 0) startBytes = progress.sentBytes; // resumed bytes don't count to speed
          const elapsed = (Date.now() - startedAt) / 1000;
          const bytesPerSecond = elapsed > 2 ? (progress.sentBytes - startBytes) / elapsed : 0;
          setState({ kind: "uploading", name: file.name, progress, bytesPerSecond });
        },
      });
      setState({ kind: "done", name: file.name });
      onUploaded();
    } catch (err) {
      setState(
        err instanceof UploadAbortedError
          ? { kind: "idle" }
          : {
              kind: "error",
              name: file.name,
              message: err instanceof Error ? err.message : "Upload failed",
            },
      );
      onUploaded(); // refresh the list so a partially uploaded video shows up
    } finally {
      await wakeLock?.release().catch(() => {});
    }
  }

  const busy = state.kind === "uploading";
  return (
    <section className="flex flex-col gap-3 rounded-xl border border-zinc-200 p-4 dark:border-zinc-800">
      <input
        ref={inputRef}
        type="file"
        accept="video/mp4,video/quicktime,.mp4,.mov,.m4v"
        className="hidden"
        onChange={start}
      />
      {busy ? (
        <UploadProgress state={state} onCancel={() => abortRef.current?.abort()} />
      ) : (
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          className="h-12 rounded-lg bg-zinc-900 font-medium text-white dark:bg-zinc-100 dark:text-zinc-900"
        >
          Upload a match video
        </button>
      )}
      {state.kind === "done" && (
        <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
          Uploaded {state.name}.
        </p>
      )}
      {state.kind === "error" && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {state.name}: {state.message}. Choose the same file again to resume where it stopped.
        </p>
      )}
      {!busy && pending.length > 0 && state.kind !== "error" && (
        <p className="text-sm text-amber-800 dark:text-amber-300">
          Unfinished upload{pending.length > 1 ? "s" : ""}: {pending.join(", ")}. Choose the same
          file to resume.
        </p>
      )}
      <p className="text-xs text-zinc-500">
        MP4 or MOV. Large files upload in pieces and can resume if your connection drops. Wi-Fi
        recommended.
      </p>
    </section>
  );
}

function UploadProgress({
  state,
  onCancel,
}: {
  state: Extract<State, { kind: "uploading" }>;
  onCancel: () => void;
}) {
  const { sentBytes, totalBytes } = state.progress;
  const pct = totalBytes ? Math.floor((sentBytes / totalBytes) * 100) : 0;
  const rate = Math.max(0, state.bytesPerSecond);
  const remaining = rate > 0 ? (totalBytes - sentBytes) / rate : null;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span className="truncate font-medium">{state.name}</span>
        <span className="shrink-0 tabular-nums">{pct}%</span>
      </div>
      <div
        className="h-2 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div className="h-full bg-emerald-600 transition-[width]" style={{ width: `${pct}%` }} />
      </div>
      <div className="flex items-center justify-between text-xs text-zinc-500">
        <span className="tabular-nums">
          {formatBytes(sentBytes)} of {formatBytes(totalBytes)}
          {rate > 0 && ` · ${formatBytes(rate)}/s`}
          {remaining !== null && ` · ~${Math.max(1, Math.ceil(remaining / 60))} min left`}
        </span>
        <button type="button" onClick={onCancel} className="font-medium underline">
          Cancel
        </button>
      </div>
      <p className="text-xs text-zinc-500">Keep this page open until the upload finishes.</p>
    </div>
  );
}

const SEP = "\u0000";

function noSubscribe(): () => void {
  return () => {};
}

async function requestWakeLock(): Promise<WakeLockSentinel | null> {
  try {
    return (await navigator.wakeLock?.request("screen")) ?? null;
  } catch {
    return null;
  }
}
