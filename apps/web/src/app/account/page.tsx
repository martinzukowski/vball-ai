import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { connection } from "next/server";
import { Suspense } from "react";

import { createSupabaseServerClient } from "@/lib/supabase/server";

import { ApiProfile } from "./api-profile";
import { SetPassword } from "./set-password";

export const metadata: Metadata = { title: "Account · VBall" };

export default function AccountPage() {
  return (
    <main className="mx-auto flex w-full max-w-xl flex-1 flex-col gap-6 px-4 py-10">
      <h1 className="text-2xl font-semibold tracking-tight">Account</h1>
      <Suspense fallback={<p className="text-zinc-500">Loading…</p>}>
        <SignedInUser />
      </Suspense>
    </main>
  );
}

async function SignedInUser() {
  // Session checks compare token expiry with the current time, so this must render per request
  // (Cache Components would otherwise try to prerender it).
  await connection();
  const supabase = await createSupabaseServerClient();
  const { data } = await supabase.auth.getClaims();
  const claims = data?.claims;
  if (!claims?.sub) {
    redirect("/login?next=/account");
  }
  return (
    <section className="flex flex-col gap-4">
      <p>
        Signed in as <b>{typeof claims.email === "string" ? claims.email : claims.sub}</b>
      </p>
      <ApiProfile />
      <SetPassword />
      <form action="/auth/signout" method="post">
        <button
          type="submit"
          className="h-11 rounded-lg border border-zinc-300 px-4 font-medium dark:border-zinc-700"
        >
          Sign out
        </button>
      </form>
    </section>
  );
}
