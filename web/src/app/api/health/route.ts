import { connection } from "next/server";

// Liveness only: unauthenticated, no secrets and no database detail. It deliberately does not touch the database,
// so a database blip cannot get the whole app (login page included) pulled out of the proxy rotation.
export async function GET() {
  await connection();
  return Response.json({ status: "ok" }, { headers: { "Cache-Control": "no-store" } });
}
