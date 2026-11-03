// Operator CLI for accounts. Works while self-service sign-up is closed because it talks to the database through
// better-auth's own internals (same password hashing and tables as the sign-up endpoint), not the HTTP API.
//
//   node scripts/user.mjs create --email <e> --password <p> [--name <n>]
//   node scripts/user.mjs set-password --email <e> --password <p>
//
// Environment: DATABASE_URL, BETTER_AUTH_SECRET (and BETTER_AUTH_URL in production).
// Exit codes: 0 done (also when the user already exists), 1 failure, 2 usage error.
import { parseArgs } from "node:util";
import { loadAuth } from "./auth-env.mjs";

const USAGE = `Usage:
  user.mjs create --email <email> --password <password> [--name <name>]
  user.mjs set-password --email <email> --password <password>
`;

function usageError(message) {
  console.error(`${message}\n\n${USAGE}`);
  process.exit(2);
}

// "pnpm user:create -- --email ..." may forward the "--" separator; drop it so parseArgs sees the flags.
const argv = process.argv.slice(2);
const sep = argv.indexOf("--");
if (sep !== -1) argv.splice(sep, 1);
const [command, ...flags] = argv;

let values;
try {
  ({ values } = parseArgs({
    args: flags,
    options: { email: { type: "string" }, password: { type: "string" }, name: { type: "string" } },
    strict: true,
    allowPositionals: false,
  }));
} catch (err) {
  usageError(err.message);
}
if (command !== "create" && command !== "set-password") usageError(`Unknown or missing command: ${command ?? "(none)"}`);
if (command === "set-password" && values.name !== undefined) usageError("--name is only valid for create");
const email = values.email?.trim().toLowerCase();
if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) usageError("--email must be a valid email address");
if (!values.password) usageError("--password is required");

const { auth, pool } = await loadAuth();

async function run() {
  const ctx = await auth.$context;
  const { minPasswordLength, maxPasswordLength } = ctx.password.config;
  if (values.password.length < minPasswordLength || values.password.length > maxPasswordLength) {
    console.error(`Password must be ${minPasswordLength} to ${maxPasswordLength} characters.`);
    return 1;
  }

  const found = await ctx.internalAdapter.findUserByEmail(email, { includeAccounts: true });
  const hasCredential = found?.accounts.some((a) => a.providerId === "credential") ?? false;

  if (command === "create") {
    if (found && hasCredential) {
      console.log(`User ${email} already exists (id ${found.user.id}); nothing changed. Use set-password to change the password.`);
      return 0;
    }
    const hash = await ctx.password.hash(values.password);
    // A user without a credential account is what an interrupted earlier run leaves behind; finish it.
    const user = found?.user ?? (await ctx.internalAdapter.createUser({ email, name: values.name || email.split("@")[0], emailVerified: false }));
    await ctx.internalAdapter.linkAccount({ userId: user.id, providerId: "credential", accountId: user.id, password: hash });
    console.log(`${found ? "Added a password to existing user" : "Created user"} ${email} (id ${user.id}).`);
    return 0;
  }

  if (!found) {
    console.error(`No user with email ${email}. Create it with: create --email ${email} --password <password>`);
    return 1;
  }
  const hash = await ctx.password.hash(values.password);
  if (hasCredential) await ctx.internalAdapter.updatePassword(found.user.id, hash);
  else await ctx.internalAdapter.linkAccount({ userId: found.user.id, providerId: "credential", accountId: found.user.id, password: hash });
  // Same as better-auth's own password reset: sessions opened with the old password end.
  await ctx.internalAdapter.deleteUserSessions(found.user.id);
  console.log(`Password updated for ${email} (id ${found.user.id}); existing sessions signed out.`);
  return 0;
}

let exitCode = 1;
try {
  exitCode = await run();
} catch (err) {
  console.error(`Failed: ${err?.message ?? err}`);
} finally {
  await pool.end().catch(() => {});
}
process.exit(exitCode);
