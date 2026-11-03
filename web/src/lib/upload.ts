"use client";
// Browser multipart upload straight to presigned part URLs (POST /v1/uploads -> PUT parts -> POST .../complete).
import type { CompletedPart, MediaRole, VreviewApi } from "./api/types";

export const PART_CONCURRENCY = 4;
export const PART_ATTEMPTS = 3;

export type PartState = { n: number; url: string; size: number; loaded: number; etag?: string; error?: string };

export interface UploadState {
  status: "idle" | "uploading" | "failed" | "done" | "cancelled";
  loaded: number;
  total: number;
  assetId?: string;
  uploadId?: string;
  partSize?: number;
  parts: PartState[];
  error?: string;
}

export const initialUpload = (file: File): UploadState => ({ status: "idle", loaded: 0, total: file.size, parts: [] });

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("Upload cancelled", "AbortError"));
    });
  });

const isAbort = (e: unknown) => e instanceof DOMException && e.name === "AbortError";

/**
 * Uploads `file`, resuming from `prev` (parts that already have an ETag are skipped, so calling this again
 * after a failure retries only the failed parts). Reports every state change through `onState`.
 */
export async function uploadFile(
  api: VreviewApi,
  file: File,
  role: MediaRole,
  prev: UploadState,
  onState: (s: UploadState) => void,
  signal: AbortSignal,
): Promise<UploadState> {
  let s: UploadState = { ...prev, status: "uploading", error: undefined, parts: prev.parts.map((p) => ({ ...p })) };
  const emit = () => {
    s = { ...s, parts: [...s.parts], loaded: s.parts.reduce((a, p) => a + (p.etag ? p.size : p.loaded), 0) };
    onState(s);
  };
  emit();

  try {
    if (!s.assetId) {
      const res = await api.createUpload({
        filename: file.name,
        size: file.size,
        content_type: file.type || null,
        role,
      });
      if (signal.aborted) {
        // Cancelled while POST /uploads was in flight: the caller never saw this asset id, so release it here.
        await api.abortUpload(res.asset_id).catch((e) => console.warn("abort upload failed", e));
        throw new DOMException("Upload cancelled", "AbortError");
      }
      s = {
        ...s,
        assetId: res.asset_id,
        uploadId: res.upload_id,
        partSize: res.part_size,
        parts: res.parts.map((p) => {
          const start = (p.part_number - 1) * res.part_size;
          return { n: p.part_number, url: p.url, size: Math.max(0, Math.min(res.part_size, file.size - start)), loaded: 0 };
        }),
      };
      emit();
    }

    const queue = s.parts.filter((p) => !p.etag).map((p) => p.n);
    const partSize = s.partSize!;

    const worker = async () => {
      while (queue.length) {
        if (signal.aborted) throw new DOMException("Upload cancelled", "AbortError");
        const n = queue.shift()!;
        const idx = s.parts.findIndex((p) => p.n === n);
        const blob = file.slice((n - 1) * partSize, Math.min(n * partSize, file.size));
        for (let attempt = 1; ; attempt++) {
          try {
            s.parts[idx] = { ...s.parts[idx], loaded: 0, error: undefined };
            emit();
            const etag = await api.putPart(s.parts[idx].url, blob, {
              signal,
              onProgress: (loaded) => {
                s.parts[idx] = { ...s.parts[idx], loaded };
                emit();
              },
            });
            s.parts[idx] = { ...s.parts[idx], loaded: blob.size, etag };
            emit();
            break;
          } catch (e) {
            if (isAbort(e) || signal.aborted) throw e;
            s.parts[idx] = { ...s.parts[idx], loaded: 0, error: (e as Error).message };
            emit();
            if (attempt >= PART_ATTEMPTS) break; // leave it failed; the user can retry failed parts
            await sleep(500 * 2 ** (attempt - 1), signal);
          }
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(PART_CONCURRENCY, queue.length) }, worker));

    const failed = s.parts.filter((p) => !p.etag);
    if (failed.length) {
      s = { ...s, status: "failed", error: `${failed.length} part${failed.length > 1 ? "s" : ""} failed: ${failed[0].error ?? "unknown error"}` };
      emit();
      return s;
    }

    const parts: CompletedPart[] = s.parts.map((p) => ({ PartNumber: p.n, ETag: p.etag! }));
    await api.completeUpload(s.assetId!, s.uploadId!, parts);
    s = { ...s, status: "done" };
    emit();
    return s;
  } catch (e) {
    s = isAbort(e) || signal.aborted ? { ...s, status: "cancelled" } : { ...s, status: "failed", error: (e as Error).message };
    emit();
    return s;
  }
}
