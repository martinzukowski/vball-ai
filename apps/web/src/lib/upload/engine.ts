// Resumable multipart upload engine. Pure logic: all I/O goes through the `UploadPort`, so it
// can run against the real API + storage in the browser and against fakes in tests.

export type Plan = { videoId: string; partSize: number; partCount: number };

export interface UploadPort {
  /** Start a new upload for this file. */
  create(file: FileLike): Promise<Plan>;
  /** Plan + part numbers already stored, for resuming; null if the upload can't be resumed. */
  resume(videoId: string): Promise<{ plan: Plan; uploaded: number[] } | null>;
  /** Presigned PUT URLs for these parts. */
  sign(videoId: string, parts: number[]): Promise<Map<number, string>>;
  /** PUT one part's bytes; reports bytes sent so far for this part. */
  put(url: string, body: Blob, onBytes: (sent: number) => void, signal: AbortSignal): Promise<void>;
  complete(videoId: string): Promise<void>;
}

export interface FileLike {
  name: string;
  size: number;
  lastModified: number;
  slice(start: number, end: number): Blob;
}

export interface PendingStore {
  get(fingerprint: string): string | null;
  set(fingerprint: string, videoId: string): void;
  clear(fingerprint: string): void;
}

export type Progress = { sentBytes: number; totalBytes: number; partsDone: number; partCount: number };

export type UploadOptions = {
  concurrency?: number;
  maxAttempts?: number;
  signBatch?: number;
  /** Re-sign URLs older than this (presigned URLs expire server-side). */
  urlMaxAgeMs?: number;
  onProgress?: (p: Progress) => void;
  signal?: AbortSignal;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
};

export class UploadAbortedError extends Error {
  constructor() {
    super("Upload cancelled");
  }
}

export function fingerprint(file: FileLike): string {
  return `${file.name}|${file.size}|${file.lastModified}`;
}

/** Uploads (or resumes) a file and completes it. Returns the video id. */
export async function uploadFile(
  file: FileLike,
  port: UploadPort,
  store: PendingStore,
  opts: UploadOptions = {},
): Promise<string> {
  const {
    concurrency = 3,
    maxAttempts = 5,
    signBatch = 20,
    urlMaxAgeMs = 45 * 60 * 1000,
    onProgress,
    signal = new AbortController().signal,
    sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
    now = () => Date.now(),
  } = opts;
  const fp = fingerprint(file);

  // 1. Resume a previous upload of the same file if storage still has it.
  let plan: Plan | null = null;
  let done = new Set<number>();
  const pendingId = store.get(fp);
  if (pendingId) {
    const resumed = await port.resume(pendingId).catch(() => null);
    if (resumed) {
      plan = resumed.plan;
      done = new Set(resumed.uploaded);
    } else {
      store.clear(fp);
    }
  }
  if (!plan) {
    plan = await port.create(file);
    store.set(fp, plan.videoId);
  }
  const { videoId, partSize, partCount } = plan;

  const partLength = (n: number) => Math.min(partSize, file.size - (n - 1) * partSize);
  const inFlight = new Map<number, number>();
  let completedBytes = 0;
  for (const n of done) completedBytes += partLength(n);
  const report = () => {
    let sent = completedBytes;
    for (const b of inFlight.values()) sent += b;
    onProgress?.({ sentBytes: sent, totalBytes: file.size, partsDone: done.size, partCount });
  };
  report();

  // 2. Upload the missing parts with bounded concurrency, signing URLs in batches.
  const queue: number[] = [];
  for (let n = 1; n <= partCount; n++) if (!done.has(n)) queue.push(n);
  const urls = new Map<number, { url: string; at: number }>();

  // Parts whose URL is being signed right now, so concurrent workers share one request.
  const signing = new Map<number, Promise<void>>();
  const fresh = (n: number) => {
    const c = urls.get(n);
    return c !== undefined && now() - c.at < urlMaxAgeMs;
  };

  async function urlFor(n: number): Promise<string> {
    if (!fresh(n)) {
      const pending = signing.get(n);
      if (pending) {
        await pending;
      }
      if (!fresh(n)) {
        const batch = [n, ...queue.filter((k) => !fresh(k) && !signing.has(k))].slice(0, signBatch);
        const request = port.sign(videoId, batch).then((signed) => {
          const at = now();
          for (const [k, url] of signed) urls.set(k, { url, at });
        });
        for (const k of batch) signing.set(k, request);
        try {
          await request;
        } finally {
          for (const k of batch) signing.delete(k);
        }
      }
    }
    const got = urls.get(n);
    if (!got) throw new Error(`No upload URL for part ${n}`);
    return got.url;
  }

  async function uploadPart(n: number) {
    const body = file.slice((n - 1) * partSize, (n - 1) * partSize + partLength(n));
    for (let attempt = 1; ; attempt++) {
      if (signal.aborted) throw new UploadAbortedError();
      try {
        const url = await urlFor(n);
        await port.put(url, body, (sent) => {
          inFlight.set(n, sent);
          report();
        }, signal);
        inFlight.delete(n);
        done.add(n);
        completedBytes += partLength(n);
        report();
        return;
      } catch (e) {
        inFlight.delete(n);
        if (signal.aborted) throw new UploadAbortedError();
        if (attempt >= maxAttempts) throw e;
        urls.delete(n); // the URL may have expired
        await sleep(Math.min(30_000, 1000 * 2 ** (attempt - 1)));
      }
    }
  }

  const workers = Array.from({ length: Math.min(concurrency, queue.length) }, async () => {
    while (queue.length) {
      const n = queue.shift()!;
      await uploadPart(n);
    }
  });
  await Promise.all(workers);

  // 3. Finish. The server verifies every part and the total size before accepting.
  await port.complete(videoId);
  store.clear(fp);
  return videoId;
}
