// Also loaded by Node directly (scripts/migrate.mjs, scripts/user.mjs) via type stripping, so keep this file
// free of path aliases, extensionless relative imports and non-erasable TypeScript (enums, parameter properties).
import { betterAuth } from "better-auth";
import { jwt } from "better-auth/plugins";
import { Pool } from "pg";

const baseURL = process.env.BETTER_AUTH_URL ?? "http://localhost:53000";
// The container speaks plain http; an https public URL means a TLS-terminating proxy (Traefik) sits in front.
const behindTlsProxy = baseURL.startsWith("https://");

/** Self-service sign-up is closed unless ALLOW_SIGNUP=1 (dev only). Accounts are created with `pnpm user:create`. */
export function isSignUpAllowed(): boolean {
  return process.env.ALLOW_SIGNUP === "1";
}

// BETTER_AUTH_URL plus optional extra origins (comma separated), e.g. a second hostname for the same app.
const trustedOrigins = [
  new URL(baseURL).origin,
  ...(process.env.BETTER_AUTH_TRUSTED_ORIGINS ?? "").split(",").map((origin) => origin.trim()),
].filter(Boolean);

export const pool = new Pool({ connectionString: process.env.DATABASE_URL });
// An idle client dropped by a database restart emits "error" on the pool; without a listener that kills the process.
pool.on("error", (err) => console.error("[auth] idle database client error:", err.message));

export const auth = betterAuth({
  baseURL,
  secret: process.env.BETTER_AUTH_SECRET,
  database: pool,
  emailAndPassword: { enabled: true, minPasswordLength: 8, disableSignUp: !isSignUpAllowed() },
  trustedOrigins,
  advanced: {
    // Cookie flags follow the public URL, not NODE_ENV, so they are right behind the proxy and on plain-http local runs.
    useSecureCookies: behindTlsProxy,
    // Behind the proxy, X-Forwarded-Host/Proto map a request back to the public origin. Client IPs for rate limiting come
    // from X-Forwarded-For (better-auth default), which Traefik sets to the single client address.
    trustedProxyHeaders: behindTlsProxy,
  },
  plugins: [
    jwt({
      // EdDSA/Ed25519 (better-auth default); the API accepts EdDSA, RS256 and ES256.
      jwt: { issuer: baseURL, audience: baseURL, expirationTime: "15m" },
    }),
  ],
});
