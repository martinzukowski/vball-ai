"use client";

import { useEffect, useState } from "react";

import { createApiClient } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { createSupabaseBrowserClient } from "@/lib/supabase/browser";

type Me = components["schemas"]["Me"];
type State = { kind: "loading" } | { kind: "ok"; me: Me } | { kind: "error"; message: string };

const GIB = 1024 ** 3;

// Calls the VBall API with the user's access token: proves the API accepts this session.
export function ApiProfile() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await createSupabaseBrowserClient().auth.getSession();
        const accessToken = data.session?.access_token;
        if (!accessToken) throw new Error("No session token");
        const result = await createApiClient().GET("/me", {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const status = result.response.status;
        if (!result.data) throw new Error(`API returned ${status}`);
        const me = result.data;
        if (!cancelled) setState({ kind: "ok", me });
      } catch (e) {
        if (!cancelled) {
          setState({ kind: "error", message: e instanceof Error ? e.message : "Request failed" });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.kind === "loading") return <p className="text-slate-500">Checking API access…</p>;
  if (state.kind === "error") {
    return (
      <p role="alert" className="text-red-700 dark:text-red-400">
        Could not reach the VBall API: {state.message}
      </p>
    );
  }
  const { me } = state;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      <dt className="text-slate-500">User ID</dt>
      <dd className="font-mono break-all">{me.id}</dd>
      <dt className="text-slate-500">Storage quota</dt>
      <dd>{(me.quota_storage_bytes / GIB).toFixed(0)} GB</dd>
      <dt className="text-slate-500">Processing quota</dt>
      <dd>{me.quota_minutes_month} min / month</dd>
    </dl>
  );
}
