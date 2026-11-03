"use client";
import { createAuthClient } from "better-auth/react";
import { jwtClient } from "better-auth/client/plugins";

export const authClient = createAuthClient({ plugins: [jwtClient()] });

let cached: { token: string; exp: number } | null = null;
let inflight: Promise<string> | null = null;

function decodeExp(token: string): number {
  try {
    const payload = JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return typeof payload.exp === "number" ? payload.exp : 0;
  } catch {
    return 0;
  }
}

/** Short-lived JWT for the vreview API, fetched from better-auth with the session cookie and cached until 60 s before expiry. */
export async function getApiToken(force = false): Promise<string> {
  const now = Date.now() / 1000;
  if (!force && cached && cached.exp - 60 > now) return cached.token;
  if (inflight) return inflight;
  inflight = (async () => {
    const res = await fetch("/api/auth/token", { credentials: "include" });
    if (!res.ok) throw new Error(`Not signed in (token endpoint returned ${res.status})`);
    const { token } = (await res.json()) as { token: string };
    cached = { token, exp: decodeExp(token) };
    return token;
  })().finally(() => {
    inflight = null;
  });
  return inflight;
}

export function clearApiToken() {
  cached = null;
}
