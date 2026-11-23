// Public runtime configuration. Read lazily so a missing value fails where it is used, with a
// clear message, instead of at import time during the build.

function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(`${name} is not set (see .env.example)`);
  }
  return value;
}

// NEXT_PUBLIC_* values must be referenced literally so Next.js can inline them into the bundle.
export function supabaseUrl(): string {
  return required("NEXT_PUBLIC_SUPABASE_URL", process.env.NEXT_PUBLIC_SUPABASE_URL);
}

export function supabasePublishableKey(): string {
  return required(
    "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY",
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY,
  );
}

export function googleSignInEnabled(): boolean {
  return process.env.NEXT_PUBLIC_AUTH_GOOGLE_ENABLED === "true";
}
