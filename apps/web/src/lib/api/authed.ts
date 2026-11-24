// API client that attaches the signed-in user's Supabase access token to every request.
import { createApiClient } from "@/lib/api/client";
import { createSupabaseBrowserClient } from "@/lib/supabase/browser";

export function createAuthedApi() {
  const api = createApiClient();
  const supabase = createSupabaseBrowserClient();
  api.use({
    async onRequest({ request }) {
      // getSession() refreshes an expired access token using the refresh token if needed.
      const { data } = await supabase.auth.getSession();
      const token = data.session?.access_token;
      if (token) request.headers.set("Authorization", `Bearer ${token}`);
      return request;
    },
  });
  return api;
}

export type AuthedApi = ReturnType<typeof createAuthedApi>;

/** Human-readable message from an API error body ({detail: string | [...]}). */
export function apiErrorMessage(error: unknown, status: number): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  }
  return `Request failed (${status})`;
}
