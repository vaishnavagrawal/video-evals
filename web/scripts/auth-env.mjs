// Shared bootstrap for the operator scripts (migrate.mjs, user.mjs): loads .env.local when present (local dev; the
// container gets real environment variables), checks the environment, then loads the same better-auth instance the
// Next.js app uses (src/lib/auth.ts, run through Node's built-in type stripping).
// The auth import is dynamic so a missing variable gets a clear message instead of a stack trace from better-auth.

export async function loadAuth() {
  try {
    // Never overrides variables that are already set, so `DATABASE_URL=... pnpm user:create` wins over the file.
    process.loadEnvFile(new URL("../.env.local", import.meta.url));
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
  }
  const required = ["DATABASE_URL", "BETTER_AUTH_SECRET"];
  // Elsewhere BETTER_AUTH_URL falls back to http://localhost:53000, which would silently give production the wrong JWT issuer.
  if (process.env.NODE_ENV === "production") required.push("BETTER_AUTH_URL");
  const missing = required.filter((name) => !process.env[name]);
  if (missing.length > 0) {
    console.error(`Missing required environment variable(s): ${missing.join(", ")}`);
    process.exit(1);
  }
  return import("../src/lib/auth.ts");
}
