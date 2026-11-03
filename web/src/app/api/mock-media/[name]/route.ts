// Mock mode only: streams the two local fixture videos with HTTP Range support so <video> can seek.
// The real app gets presigned storage URLs from GET /v1/reviews/{run_id}/media/{asset_id} instead.
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { Readable } from "node:stream";

const FILES: Record<string, string | undefined> = {
  original: process.env.VREVIEW_MOCK_ORIGINAL_PATH,
  candidate: process.env.VREVIEW_MOCK_CANDIDATE_PATH,
};

export async function GET(req: Request, ctx: RouteContext<"/api/mock-media/[name]">) {
  const { name } = await ctx.params;
  const path = FILES[name];
  if (process.env.NEXT_PUBLIC_VREVIEW_MOCK !== "1" || !path) return new Response("Not found", { status: 404 });
  let size: number;
  try {
    size = (await stat(path)).size;
  } catch {
    return new Response("Mock media file missing", { status: 404 });
  }
  const headers: Record<string, string> = { "Content-Type": "video/mp4", "Accept-Ranges": "bytes" };
  const m = /^bytes=(\d*)-(\d*)$/.exec(req.headers.get("range") ?? "");
  if (!m) {
    const body = Readable.toWeb(createReadStream(path)) as ReadableStream;
    return new Response(body, { headers: { ...headers, "Content-Length": String(size) } });
  }
  let start = m[1] ? Number(m[1]) : size - Number(m[2]);
  let end = m[1] && m[2] ? Number(m[2]) : size - 1;
  start = Math.max(0, start);
  end = Math.min(end, size - 1, start + 8 * 1024 * 1024 - 1);
  if (start > end) return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
  const body = Readable.toWeb(createReadStream(path, { start, end })) as ReadableStream;
  return new Response(body, {
    status: 206,
    headers: { ...headers, "Content-Range": `bytes ${start}-${end}/${size}`, "Content-Length": String(end - start + 1) },
  });
}
