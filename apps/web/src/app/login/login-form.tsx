"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { googleSignInEnabled } from "@/lib/env";
import { createSupabaseBrowserClient } from "@/lib/supabase/browser";

type Status =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "sent"; email: string }
  | { kind: "error"; message: string };

export function LoginForm() {
  const params = useSearchParams();
  const next = params.get("next") ?? "/account";
  const linkError = params.get("error") === "link";
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [usePassword, setUsePassword] = useState(false);
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  const callbackUrl = () =>
    `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`;

  async function signInWithPassword() {
    setStatus({ kind: "sending" });
    const { error } = await createSupabaseBrowserClient().auth.signInWithPassword({
      email,
      password,
    });
    if (error) {
      setStatus({ kind: "error", message: error.message });
      return;
    }
    router.replace(next.startsWith("/") && !next.startsWith("//") ? next : "/account");
    router.refresh();
  }

  async function sendLink(e: FormEvent) {
    e.preventDefault();
    if (usePassword) return signInWithPassword();
    setStatus({ kind: "sending" });
    const { error } = await createSupabaseBrowserClient().auth.signInWithOtp({
      email,
      options: { emailRedirectTo: callbackUrl() },
    });
    setStatus(error ? { kind: "error", message: error.message } : { kind: "sent", email });
  }

  async function signInWithGoogle() {
    const { error } = await createSupabaseBrowserClient().auth.signInWithOAuth({
      provider: "google",
      options: { redirectTo: callbackUrl() },
    });
    if (error) setStatus({ kind: "error", message: error.message });
  }

  if (status.kind === "sent") {
    return (
      <p role="status" className="rounded-lg bg-emerald-50 p-4 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100">
        Check <b>{status.email}</b> for a sign-in link. Open it on this device, in this browser.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {linkError && (
        <p role="alert" className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
          That sign-in link didn&apos;t work. It may have expired or been opened in a different
          browser. Request a new one below.
        </p>
      )}
      <form onSubmit={sendLink} className="flex flex-col gap-3">
        <label htmlFor="email" className="text-sm font-medium">
          Email
        </label>
        <input
          id="email"
          type="email"
          required
          autoComplete="email"
          inputMode="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="h-12 rounded-lg border border-zinc-300 bg-white px-3 text-base dark:border-zinc-700 dark:bg-zinc-900"
        />
        {usePassword && (
          <>
            <label htmlFor="password" className="text-sm font-medium">
              Password
            </label>
            <input
              id="password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="h-12 rounded-lg border border-zinc-300 bg-white px-3 text-base dark:border-zinc-700 dark:bg-zinc-900"
            />
          </>
        )}
        <button
          type="submit"
          disabled={status.kind === "sending"}
          className="h-12 rounded-lg bg-zinc-900 font-medium text-white disabled:opacity-60 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {status.kind === "sending"
            ? usePassword
              ? "Signing in…"
              : "Sending…"
            : usePassword
              ? "Sign in"
              : "Email me a sign-in link"}
        </button>
        <button
          type="button"
          onClick={() => {
            setUsePassword((v) => !v);
            setStatus({ kind: "idle" });
          }}
          className="text-sm text-zinc-500 underline"
        >
          {usePassword ? "Email me a link instead" : "Sign in with a password instead"}
        </button>
      </form>
      {googleSignInEnabled() && (
        <button
          type="button"
          onClick={signInWithGoogle}
          className="h-12 rounded-lg border border-zinc-300 font-medium dark:border-zinc-700"
        >
          Continue with Google
        </button>
      )}
      {status.kind === "error" && (
        <p role="alert" className="text-sm text-red-700 dark:text-red-400">
          {status.message}
        </p>
      )}
    </div>
  );
}
