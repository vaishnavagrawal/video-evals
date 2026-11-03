"use client";
import { clearApiToken, getApiToken } from "@/lib/auth-client";
import { ApiError, type CompletedPart, type FeedbackRow, type PutPart, type VreviewApi } from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_VREVIEW_API_URL || "http://localhost:58000").replace(/\/+$/, "");

async function request<T>(method: string, path: string, body?: unknown, retried = false): Promise<T> {
  const token = await getApiToken(retried);
  const res = await fetch(`${API_URL}/v1${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && !retried) {
    clearApiToken();
    return request<T>(method, path, body, true);
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j?.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  return (await res.json()) as T;
}

const enc = encodeURIComponent;

/** PUT one part to a presigned URL with upload progress (fetch has no upload progress, so XHR). */
export const xhrPutPart: PutPart = (url, body, { onProgress, signal }) =>
  new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const etag = xhr.getResponseHeader("ETag");
        // Storage CORS must expose ETag (MinIO/R2 bucket CORS: ExposeHeaders ETag).
        if (!etag) reject(new Error("Upload succeeded but the ETag header is not readable (check bucket CORS ExposeHeaders)"));
        else resolve(etag);
      } else reject(new Error(`Part upload failed: HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("Network error during part upload"));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    if (signal.aborted) return xhr.abort();
    signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(body);
  });

export const realApi: VreviewApi = {
  listReviews: (limit = 50, offset = 0) => request("GET", `/reviews?limit=${limit}&offset=${offset}`),
  getReview: (runId) => request("GET", `/reviews/${enc(runId)}`),
  getMedia: (runId, assetId) => request("GET", `/reviews/${enc(runId)}/media/${enc(assetId)}`),
  createUpload: (req) => request("POST", "/uploads", req),
  completeUpload: async (assetId, uploadId, parts: CompletedPart[]) => {
    await request("POST", `/uploads/${enc(assetId)}/complete`, { upload_id: uploadId, parts });
  },
  createReview: (req) => request("POST", "/reviews", req),
  sendFeedback: (runId, issueId, verdict, comment) =>
    request("POST", `/reviews/${enc(runId)}/issues/${enc(issueId)}/feedback`, { verdict, comment: comment ?? null }),
  listFeedback: async (runId) => {
    try {
      return await request<FeedbackRow[]>("GET", `/reviews/${enc(runId)}/feedback`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return [];
      throw e;
    }
  },
  abortUpload: async (assetId) => {
    try {
      await request("POST", `/uploads/${enc(assetId)}/abort`);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 409)) return;
      throw e;
    }
  },
  putPart: xhrPutPart,
};
