// Typed client for the VBall API. Types are generated from the API's OpenAPI schema:
//   uv run vball-api-openapi apps/web/openapi.json && pnpm api:types
// CI regenerates both files and fails if they differ from what is committed.
import createClient from "openapi-fetch";

import type { paths } from "./schema";

export function createApiClient(baseUrl: string = apiBaseUrl()) {
  return createClient<paths>({ baseUrl });
}

function apiBaseUrl(): string {
  const url = process.env.NEXT_PUBLIC_API_URL;
  if (!url) {
    throw new Error("NEXT_PUBLIC_API_URL is not set");
  }
  return url;
}
