// Signs up (or signs in) a test user against the running app, fetches a JWT and verifies it against
// /api/auth/jwks with the expected issuer/audience. Usage: node scripts/verify-auth.mjs [baseUrl]
import { createRemoteJWKSet, jwtVerify, decodeProtectedHeader } from "jose";

const base = process.argv[2] ?? "http://localhost:53000";
const email = process.env.TEST_EMAIL ?? "test@vreview.local";
const password = process.env.TEST_PASSWORD ?? "test-password-123";

async function post(path, body) {
  return fetch(base + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: base },
    body: JSON.stringify(body),
  });
}

let res = await post("/api/auth/sign-up/email", { email, password, name: "Test User" });
if (!res.ok) {
  console.log(`sign-up: ${res.status} (${(await res.text()).slice(0, 120)}), trying sign-in`);
  res = await post("/api/auth/sign-in/email", { email, password });
}
if (!res.ok) throw new Error(`sign-in failed: ${res.status} ${await res.text()}`);
const cookie = res.headers.getSetCookie().map((c) => c.split(";")[0]).join("; ");
console.log("session cookie:", cookie.split("=")[0]);

const tok = await fetch(base + "/api/auth/token", { headers: { cookie } });
if (!tok.ok) throw new Error(`token: ${tok.status} ${await tok.text()}`);
const { token } = await tok.json();

const jwks = await (await fetch(base + "/api/auth/jwks")).json();
console.log("jwks keys:", jwks.keys.map((k) => `${k.kid} ${k.alg}/${k.crv ?? k.kty}`).join(", "));

const { payload } = await jwtVerify(token, createRemoteJWKSet(new URL(base + "/api/auth/jwks")), {
  issuer: base,
  audience: base,
});
console.log("header:", JSON.stringify(decodeProtectedHeader(token)));
console.log("claims:", JSON.stringify({ sub: payload.sub, iss: payload.iss, aud: payload.aud, exp: payload.exp, email: payload.email }));
console.log("OK: token verifies against JWKS with iss/aud =", base);
if (process.env.PRINT_TOKEN) console.log(token);
