"use client";

import { useState, type FormEvent } from "react";

import { createSupabaseBrowserClient } from "@/lib/supabase/browser";

/** Lets the signed-in user set a password, so later sign-ins don't need an email link
 *  (Supabase's built-in email sender is limited to a few emails per hour). */
export function SetPassword() {
  const [password, setPassword] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (password.length < 8) {
      setMsg({ ok: false, text: "Use at least 8 characters." });
      return;
    }
    setBusy(true);
    const { error } = await createSupabaseBrowserClient().auth.updateUser({ password });
    setBusy(false);
    setPassword("");
    setMsg(
      error
        ? { ok: false, text: error.message }
        : { ok: true, text: "Password saved. Next time choose “Sign in with a password”." },
    );
  }

  return (
    <form onSubmit={save} className="flex flex-col gap-2 rounded-xl border border-slate-200 bg-white shadow-sm p-3 dark:border-slate-800">
      <label htmlFor="new-password" className="text-sm font-medium">
        Set a password (optional)
      </label>
      <div className="flex gap-2">
        <input
          id="new-password"
          type="password"
          autoComplete="new-password"
          minLength={8}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="h-10 flex-1 rounded-lg border border-slate-300 bg-white px-3 dark:border-slate-700 dark:bg-slate-900"
        />
        <button
          type="submit"
          disabled={busy}
          className="h-10 rounded-lg border border-slate-300 px-3 text-sm font-medium disabled:opacity-50 dark:border-slate-700"
        >
          Save
        </button>
      </div>
      {msg && (
        <p role={msg.ok ? "status" : "alert"} className={`text-sm ${msg.ok ? "text-emerald-600" : "text-red-600"}`}>
          {msg.text}
        </p>
      )}
      <p className="text-xs text-slate-500">
        Lets you sign in without waiting for an email link.
      </p>
    </form>
  );
}
