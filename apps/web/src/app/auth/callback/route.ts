// Landing point for magic links and OAuth: exchanges the one-time code for a session cookie.
import { NextResponse, type NextRequest } from "next/server";

import { createSupabaseServerClient, safeNextPath } from "@/lib/supabase/server";

export async function GET(request: NextRequest) {
  const { searchParams, origin } = request.nextUrl;
  const code = searchParams.get("code");
  const next = safeNextPath(searchParams.get("next"));

  if (code) {
    const supabase = await createSupabaseServerClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) {
      return NextResponse.redirect(`${origin}${next}`);
    }
  }
  // Common cause: the link was opened in a different browser than the one that requested it.
  return NextResponse.redirect(`${origin}/login?error=link`);
}
