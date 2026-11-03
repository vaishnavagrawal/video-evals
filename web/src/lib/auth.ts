import { betterAuth } from "better-auth";
import { jwt } from "better-auth/plugins";
import { Pool } from "pg";

const baseURL = process.env.BETTER_AUTH_URL ?? "http://localhost:53000";

export const auth = betterAuth({
  baseURL,
  secret: process.env.BETTER_AUTH_SECRET,
  database: new Pool({ connectionString: process.env.DATABASE_URL }),
  emailAndPassword: { enabled: true, minPasswordLength: 8 },
  trustedOrigins: [baseURL],
  plugins: [
    jwt({
      // EdDSA/Ed25519 (better-auth default); the API accepts EdDSA, RS256 and ES256.
      jwt: { issuer: baseURL, audience: baseURL, expirationTime: "15m" },
    }),
  ],
});
