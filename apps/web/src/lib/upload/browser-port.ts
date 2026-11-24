// Browser implementation of the upload engine's I/O: VBall API for planning/signing/completing,
// XMLHttpRequest for the part PUTs (fetch can't report upload progress), localStorage for the
// "unfinished upload" marker used to resume after a reload.
import { apiErrorMessage, type AuthedApi } from "@/lib/api/authed";

import type { FileLike, PendingStore, Plan, UploadPort } from "./engine";

export class ApiError extends Error {}

export function browserPort(api: AuthedApi): UploadPort {
  return {
    async create(file: FileLike): Promise<Plan> {
      const { data, error, response } = await api.POST("/videos/uploads", {
        body: { filename: file.name, size_bytes: file.size, content_type: contentType(file) },
      });
      if (!data) throw new ApiError(apiErrorMessage(error, response.status));
      return {
        videoId: data.video.id,
        partSize: data.part_size_bytes,
        partCount: data.part_count,
      };
    },
    async resume(videoId) {
      const { data } = await api.GET("/videos/{video_id}/upload", {
        params: { path: { video_id: videoId } },
      });
      if (!data || data.video.status !== "uploading") return null;
      if (new Date(data.expires_at).getTime() < Date.now()) return null;
      return {
        plan: { videoId, partSize: data.part_size_bytes, partCount: data.part_count },
        uploaded: data.uploaded_parts.map((p) => p.part_number),
      };
    },
    async sign(videoId, parts) {
      const { data, error, response } = await api.POST("/videos/{video_id}/upload/parts", {
        params: { path: { video_id: videoId } },
        body: { part_numbers: parts },
      });
      if (!data) throw new ApiError(apiErrorMessage(error, response.status));
      return new Map(data.urls.map((u) => [u.part_number, u.url]));
    },
    put(url, body, onBytes, signal) {
      return new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", url);
        xhr.upload.onprogress = (e) => onBytes(e.loaded);
        xhr.onload = () =>
          xhr.status >= 200 && xhr.status < 300
            ? resolve()
            : reject(new Error(`Storage returned ${xhr.status}`));
        xhr.onerror = () => reject(new Error("Network error while uploading"));
        xhr.ontimeout = () => reject(new Error("Upload timed out"));
        xhr.timeout = 10 * 60 * 1000;
        const abort = () => xhr.abort();
        signal.addEventListener("abort", abort, { once: true });
        xhr.onabort = () => reject(new Error("aborted"));
        xhr.send(body);
      });
    },
    async complete(videoId) {
      const { data, error, response } = await api.POST("/videos/{video_id}/upload/complete", {
        params: { path: { video_id: videoId } },
      });
      if (!data) throw new ApiError(apiErrorMessage(error, response.status));
    },
  };
}

function contentType(file: FileLike): string {
  const type = (file as { type?: string }).type ?? "";
  return type === "video/mp4" || type === "video/quicktime" ? type : "";
}

const PREFIX = "vball:pending-upload:";

export const localPendingStore: PendingStore = {
  get(fp) {
    try {
      return localStorage.getItem(PREFIX + fp);
    } catch {
      return null;
    }
  },
  set(fp, videoId) {
    try {
      localStorage.setItem(PREFIX + fp, videoId);
    } catch {
      // Private mode etc.: resume just won't be available.
    }
  },
  clear(fp) {
    try {
      localStorage.removeItem(PREFIX + fp);
    } catch {}
  },
};

/** Unfinished uploads remembered in this browser (file name + video id), for the UI hint. */
export function pendingUploads(): { name: string; videoId: string }[] {
  const out: { name: string; videoId: string }[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key?.startsWith(PREFIX)) continue;
      const [name] = key.slice(PREFIX.length).split("|");
      out.push({ name, videoId: localStorage.getItem(key) ?? "" });
    }
  } catch {}
  return out;
}
