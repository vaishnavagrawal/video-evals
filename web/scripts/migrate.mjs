// Applies the better-auth schema (user, session, account, verification, jwks) with better-auth's own migrator.
// Safe on every start: a no-op when the schema is current, serialised across concurrent starters by a Postgres
// advisory lock, and it waits for a database that is still booting.
// Usage: node scripts/migrate.mjs   (DATABASE_URL, BETTER_AUTH_SECRET; BETTER_AUTH_URL in production)
import { setTimeout as sleep } from "node:timers/promises";
import pg from "pg";
import { getMigrations } from "better-auth/db/migration";
import { loadAuth } from "./auth-env.mjs";

const { auth, pool } = await loadAuth();

const TRANSIENT = new Set(["ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "ENOTFOUND", "EAI_AGAIN", "57P03"]);
const ATTEMPTS = 15;
const DELAY_MS = 2000;

async function connectWithRetry() {
  for (let attempt = 1; ; attempt++) {
    const client = new pg.Client({ connectionString: process.env.DATABASE_URL });
    try {
      await client.connect();
      return client;
    } catch (err) {
      await client.end().catch(() => {});
      if (!TRANSIENT.has(err.code) || attempt >= ATTEMPTS) throw err;
      console.error(`[migrate] database not ready (${err.code}), retry ${attempt}/${ATTEMPTS - 1} in ${DELAY_MS / 1000}s`);
      await sleep(DELAY_MS);
    }
  }
}

let exitCode = 0;
const lockClient = await connectWithRetry().catch((err) => {
  console.error(`[migrate] cannot connect to the database: ${err.code ?? ""} ${err.message}`);
  process.exit(1);
});
try {
  // Session-level lock on a dedicated connection: released by unlock below or when the connection closes.
  await lockClient.query("select pg_advisory_lock(hashtext('vreview_auth_migrate'))");
  const { toBeCreated, toBeAdded, toBeAddedIndexes, schemaProblems, runMigrations } = await getMigrations(auth.options);
  for (const problem of schemaProblems) console.error(`[migrate] schema problem (no migration fixes this): ${problem}`);
  if (toBeCreated.length + toBeAdded.length + toBeAddedIndexes.length === 0) {
    console.log("[migrate] auth schema is up to date");
  } else {
    if (toBeCreated.length) console.log(`[migrate] creating tables: ${toBeCreated.map((t) => t.table).join(", ")}`);
    if (toBeAdded.length) console.log(`[migrate] adding columns to: ${toBeAdded.map((t) => t.table).join(", ")}`);
    if (toBeAddedIndexes.length) console.log(`[migrate] adding indexes: ${toBeAddedIndexes.map((i) => i.name).join(", ")}`);
    await runMigrations();
    console.log("[migrate] auth schema migrated");
  }
} catch (err) {
  console.error(`[migrate] failed: ${err?.message ?? err}`);
  exitCode = 1;
} finally {
  await lockClient.query("select pg_advisory_unlock(hashtext('vreview_auth_migrate'))").catch(() => {});
  await lockClient.end().catch(() => {});
  await pool.end().catch(() => {});
}
process.exit(exitCode);
