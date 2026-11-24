import { describe, expect, it } from "vitest";

import {
  fingerprint,
  uploadFile,
  UploadAbortedError,
  type FileLike,
  type PendingStore,
  type Plan,
  type UploadPort,
} from "./engine";

function fakeFile(size: number, name = "match.mov"): FileLike {
  const bytes = new Uint8Array(size).map((_, i) => i % 251);
  const blob = new Blob([bytes]);
  return { name, size, lastModified: 1, slice: (s, e) => blob.slice(s, e) };
}

function memoryStore(): PendingStore & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    get: (k) => data.get(k) ?? null,
    set: (k, v) => void data.set(k, v),
    clear: (k) => void data.delete(k),
  };
}

/** In-memory "server": records stored parts, can fail PUTs on demand. */
function fakeServer(partSize: number) {
  const stored = new Map<string, Map<number, Uint8Array>>();
  const signCalls: number[][] = [];
  let failPuts = 0;
  let completed: string | null = null;
  let plan: Plan | null = null;
  const port: UploadPort = {
    async create(file) {
      plan = { videoId: "v1", partSize, partCount: Math.ceil(file.size / partSize) };
      stored.set("v1", new Map());
      return plan;
    },
    async resume(id) {
      if (!plan || id !== plan.videoId) return null;
      return { plan, uploaded: [...(stored.get(id)?.keys() ?? [])] };
    },
    async sign(id, parts) {
      signCalls.push(parts);
      return new Map(parts.map((n) => [n, `https://s3/${id}/${n}`]));
    },
    async put(url, body, onBytes, signal) {
      if (signal.aborted) throw new Error("aborted");
      if (failPuts > 0) {
        failPuts--;
        throw new Error("network");
      }
      const [, , , id, n] = url.split("/");
      onBytes(body.size);
      stored.get(id)!.set(Number(n), new Uint8Array(await body.arrayBuffer()));
    },
    async complete(id) {
      completed = id;
    },
  };
  return {
    port,
    stored,
    signCalls,
    failNext: (n: number) => void (failPuts = n),
    get completed() {
      return completed;
    },
  };
}

const noSleep = { sleep: async () => {} };

describe("uploadFile", () => {
  it("uploads every part exactly with the right bytes and completes", async () => {
    const file = fakeFile(1000);
    const server = fakeServer(300);
    const progress: number[] = [];
    const store = memoryStore();
    const id = await uploadFile(file, server.port, store, {
      ...noSleep,
      onProgress: (p) => progress.push(p.sentBytes),
    });
    expect(id).toBe("v1");
    expect(server.completed).toBe("v1");
    const parts = server.stored.get("v1")!;
    expect([...parts.keys()].sort()).toEqual([1, 2, 3, 4]);
    expect(parts.get(4)!.length).toBe(100);
    const joined = new Uint8Array(1000);
    for (const [n, b] of parts) joined.set(b, (n - 1) * 300);
    expect(joined).toEqual(new Uint8Array(await file.slice(0, 1000).arrayBuffer()));
    expect(progress.at(-1)).toBe(1000);
    expect(store.data.size).toBe(0); // pending marker cleared
  });

  it("retries failed parts with backoff", async () => {
    const server = fakeServer(300);
    server.failNext(3);
    await uploadFile(fakeFile(1000), server.port, memoryStore(), noSleep);
    expect(server.stored.get("v1")!.size).toBe(4);
  });

  it("gives up after maxAttempts and keeps the pending marker for a later resume", async () => {
    const server = fakeServer(300);
    server.failNext(100);
    const store = memoryStore();
    const file = fakeFile(1000);
    await expect(
      uploadFile(file, server.port, store, { ...noSleep, maxAttempts: 2, concurrency: 1 }),
    ).rejects.toThrow("network");
    expect(store.get(fingerprint(file))).toBe("v1");
    expect(server.completed).toBeNull();
  });

  it("resumes: only uploads parts storage doesn't have", async () => {
    const file = fakeFile(1000);
    const server = fakeServer(300);
    const store = memoryStore();
    server.failNext(100);
    await uploadFile(file, server.port, store, { ...noSleep, maxAttempts: 1, concurrency: 1 }).catch(
      () => {},
    );
    // Pretend parts 1 and 3 had made it before the failure.
    server.stored.get("v1")!.set(1, new Uint8Array(300));
    server.stored.get("v1")!.set(3, new Uint8Array(300));
    server.failNext(0);
    server.signCalls.length = 0;
    await uploadFile(file, server.port, store, noSleep);
    expect(server.signCalls.flat().sort()).toEqual([2, 4]);
    expect(server.completed).toBe("v1");
  });

  it("starts over when the pending upload can't be resumed", async () => {
    const file = fakeFile(500);
    const server = fakeServer(300);
    const store = memoryStore();
    store.set(fingerprint(file), "gone");
    await uploadFile(file, server.port, store, noSleep);
    expect(server.completed).toBe("v1");
  });

  it("stops promptly when cancelled", async () => {
    const server = fakeServer(300);
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(
      uploadFile(fakeFile(1000), server.port, memoryStore(), { ...noSleep, signal: ctrl.signal }),
    ).rejects.toBeInstanceOf(UploadAbortedError);
    expect(server.completed).toBeNull();
  });

  it("re-signs URLs that are too old", async () => {
    let clock = 0;
    const server = fakeServer(300);
    const origPut = server.port.put;
    server.port.put = async (...args) => {
      clock += 60 * 60 * 1000; // each PUT "takes" an hour
      return origPut(...args);
    };
    await uploadFile(fakeFile(1000), server.port, memoryStore(), {
      ...noSleep,
      concurrency: 1,
      now: () => clock,
    });
    expect(server.signCalls.length).toBe(4); // one signing call per part
  });
});
