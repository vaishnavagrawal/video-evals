"use client";
import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";
import { api, type MediaRole } from "@/lib/api";
import { initialUpload, uploadFile, type UploadState } from "@/lib/upload";
import { ArrowCounterClockwiseIcon, FileAudioIcon, FileVideoIcon, ImageIcon, TrashIcon, UploadSimpleIcon } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ErrorBox, cx, fmtBytes } from "./common";

/** Radix Select has no empty value, so "auto-detect" uses a sentinel that maps to "". */
const AUTO = "auto";

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"];
const IMAGE_EXT = /\.(png|jpe?g|webp)$/i;
const MEDIA_EXT = /\.(mp4|m4v|mov|mkv|webm|avi|mxf|ts|wav|mp3|m4a|aac|flac|ogg|opus)$/i;

type Kind = "video" | "audio" | "image";

function kindOf(f: File): Kind | null {
  if (f.type.startsWith("video/")) return "video";
  if (f.type.startsWith("audio/")) return "audio";
  if (IMAGE_TYPES.includes(f.type) || (!f.type && IMAGE_EXT.test(f.name))) return "image";
  if (!f.type && MEDIA_EXT.test(f.name)) return /\.(wav|mp3|m4a|aac|flac|ogg|opus)$/i.test(f.name) ? "audio" : "video";
  return null;
}

interface Item {
  id: string;
  file: File;
  kind: Kind;
  role: MediaRole;
  preview?: string;
  upload: UploadState;
}

export const LANGUAGES: [string, string][] = [
  ["ar", "Arabic"],
  ["zh", "Chinese"],
  ["nl", "Dutch"],
  ["en", "English"],
  ["fr", "French"],
  ["de", "German"],
  ["hi", "Hindi"],
  ["id", "Indonesian"],
  ["it", "Italian"],
  ["ja", "Japanese"],
  ["ko", "Korean"],
  ["pl", "Polish"],
  ["pt", "Portuguese"],
  ["pt-BR", "Portuguese (Brazil)"],
  ["ru", "Russian"],
  ["es", "Spanish"],
  ["es-419", "Spanish (Latin America)"],
  ["ta", "Tamil"],
  ["te", "Telugu"],
  ["th", "Thai"],
  ["tr", "Turkish"],
  ["vi", "Vietnamese"],
];

let counter = 0;

function DropZone({
  label,
  hint,
  accept,
  multiple,
  onFiles,
  compact,
}: {
  label: string;
  hint: string;
  accept: string;
  multiple?: boolean;
  onFiles: (f: File[]) => void;
  compact?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const hintId = useId();
  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        onFiles([...e.dataTransfer.files]);
      }}
      className={cx(
        "border border-dashed text-center transition-colors",
        compact ? "py-5" : "py-10",
        over ? "border-interactive bg-highlight" : "border-border-strong bg-muted",
      )}
    >
      <input
        ref={input}
        type="file"
        accept={accept}
        multiple={multiple}
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(e) => {
          onFiles([...(e.target.files ?? [])]);
          e.target.value = "";
        }}
      />
      {!compact && <UploadSimpleIcon className="mx-auto mb-2 size-6 text-muted-foreground" aria-hidden />}
      <p className="text-sm">
        Drag and drop here, or{" "}
        <button
          type="button"
          aria-describedby={hintId}
          className="text-link font-medium hover:underline"
          onClick={() => input.current?.click()}
        >
          {label}
        </button>
      </p>
      <p id={hintId} className="text-xs text-muted-foreground mt-1">
        {hint}
      </p>
    </div>
  );
}

function ProgressRow({ u }: { u: UploadState }) {
  if (u.status === "idle") return null;
  const pct = u.total ? Math.round((100 * u.loaded) / u.total) : 0;
  const done = u.parts.filter((p) => p.etag).length;
  const retrying = u.parts.filter((p) => p.error && !p.etag).length;
  return (
    <div className="mt-1.5">
      <div
        className="h-1 bg-border-subtle overflow-hidden"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-label="Upload progress"
      >
        <div
          className={cx(
            "h-full transition-[width] duration-150",
            u.status === "failed" ? "bg-blocker" : u.status === "done" ? "bg-success" : u.status === "cancelled" ? "bg-nit" : "bg-interactive",
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex justify-between text-xs text-muted-foreground mt-0.5 tabular-nums">
        <span>
          {u.status === "done"
            ? "Uploaded"
            : u.status === "cancelled"
              ? "Cancelled"
              : u.status === "failed"
                ? "Upload failed"
                : `Uploading… ${fmtBytes(u.loaded)} of ${fmtBytes(u.total)}`}
          {u.parts.length > 1 && ` · parts ${done}/${u.parts.length}`}
          {u.status === "uploading" && retrying > 0 && ` · retrying ${retrying}`}
        </span>
        <span>{pct}%</span>
      </div>
      {u.status === "failed" && u.error && <p className="text-xs text-blocker mt-0.5">{u.error}</p>}
    </div>
  );
}

function FileRow({
  item,
  locked,
  onRole,
  onRemove,
  onRetry,
  roleOptions,
}: {
  item: Item;
  locked: boolean;
  onRole?: (r: MediaRole) => void;
  onRemove: () => void;
  onRetry?: () => void;
  roleOptions?: { value: MediaRole; label: string }[];
}) {
  const roleId = useId();
  return (
    <li className="flex items-start gap-3 px-3 py-2.5">
      {item.preview ? (
        // eslint-disable-next-line @next/next/no-img-element -- local object URL thumbnail
        <img src={item.preview} alt="" className="size-12 border object-cover bg-muted" />
      ) : (
        <span className="size-12 border bg-muted inline-flex items-center justify-center text-muted-foreground" title={item.kind}>
          {item.kind === "audio" ? <FileAudioIcon className="size-5" /> : item.kind === "image" ? <ImageIcon className="size-5" /> : <FileVideoIcon className="size-5" />}
          <span className="sr-only">{item.kind}</span>
        </span>
      )}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium truncate">{item.file.name}</span>
          <span className="text-xs text-muted-foreground shrink-0">{fmtBytes(item.file.size)}</span>
        </div>
        {item.kind === "image" && (
          <p className="text-xs text-muted-foreground">Reference image: stored with the review, not used by the dubbing evaluator yet.</p>
        )}
        <ProgressRow u={item.upload} />
      </div>
      {roleOptions && onRole && (
        <div className="shrink-0">
          <Select value={item.role} disabled={locked || roleOptions.length < 2} onValueChange={(v) => onRole(v as MediaRole)}>
            <SelectTrigger id={roleId} size="sm" aria-label={`Role for ${item.file.name}`} className="bg-field">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {roleOptions.map((o) => (
                <SelectItem key={o.value} value={o.value}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
      {item.upload.status === "failed" && onRetry && !locked && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <ArrowCounterClockwiseIcon data-icon="inline-start" />
          Retry failed parts
        </Button>
      )}
      <Button variant="ghost" size="sm" onClick={onRemove} disabled={locked} aria-label={`Remove ${item.file.name}`}>
        <TrashIcon data-icon="inline-start" />
        Remove
      </Button>
    </li>
  );
}

export function NewReview() {
  const router = useRouter();
  const formId = useId();
  const [candidate, setCandidate] = useState<Item | null>(null);
  const [refs, setRefs] = useState<Item[]>([]);
  const [src, setSrc] = useState("");
  const [tgt, setTgt] = useState("");
  const [phase, setPhase] = useState<"edit" | "uploading" | "creating" | "failed">("edit");
  const [error, setError] = useState<string | null>(null);
  const [rejected, setRejected] = useState<string[]>([]);
  const abort = useRef<AbortController | null>(null);
  const previews = useRef(new Set<string>());

  useEffect(() => {
    const set = previews.current;
    return () => set.forEach((u) => URL.revokeObjectURL(u));
  }, []);

  const makeItem = (file: File, kind: Kind, role: MediaRole): Item => {
    const preview = kind === "image" ? URL.createObjectURL(file) : undefined;
    if (preview) previews.current.add(preview);
    return { id: `f${++counter}`, file, kind, role, preview, upload: initialUpload(file) };
  };
  const dropPreview = (it: Item | null) => {
    if (it?.preview) {
      URL.revokeObjectURL(it.preview);
      previews.current.delete(it.preview);
    }
  };

  const locked = phase === "uploading" || phase === "creating";

  function addCandidate(files: File[]) {
    const f = files[0];
    if (!f) return;
    const k = kindOf(f);
    if (k !== "video" && k !== "audio") {
      setRejected([`${f.name}: the candidate must be a video or audio file.`]);
      return;
    }
    setRejected([]);
    dropPreview(candidate);
    setCandidate(makeItem(f, k, "candidate"));
  }

  function addRefs(files: File[]) {
    const bad: string[] = [];
    const add: Item[] = [];
    let hasOriginal = refs.some((r) => r.role === "original");
    for (const f of files) {
      const k = kindOf(f);
      if (!k) {
        bad.push(`${f.name}: unsupported type. Use a video/audio file, or a PNG, JPEG or WebP image.`);
        continue;
      }
      const role: MediaRole = k === "image" ? "reference" : hasOriginal ? "reference" : "original";
      if (role === "original") hasOriginal = true;
      add.push(makeItem(f, k, role));
    }
    setRejected(bad);
    setRefs((r) => [...r, ...add]);
  }

  const patch = (id: string, upd: (it: Item) => Item) => {
    if (candidate?.id === id) setCandidate((c) => (c ? upd(c) : c));
    else setRefs((rs) => rs.map((r) => (r.id === id ? upd(r) : r)));
  };

  async function runUploads(items: Item[], signal: AbortSignal) {
    const results = await Promise.all(
      items.map((it) =>
        it.upload.status === "done"
          ? Promise.resolve(it.upload)
          : uploadFile(
              api,
              it.file,
              it.role,
              it.upload,
              // After cancel() resets the rows, late updates from the aborted upload are dropped.
              (s) => !signal.aborted && patch(it.id, (x) => ({ ...x, upload: s })),
              signal,
            ),
      ),
    );
    return results;
  }

  async function submit(e?: React.FormEvent) {
    e?.preventDefault();
    if (!candidate) return;
    setError(null);
    const items = [candidate, ...refs];
    const ctrl = new AbortController();
    abort.current = ctrl;
    setPhase("uploading");
    const results = await runUploads(items, ctrl.signal);
    // cancel() already reset the rows and the phase; a stale continuation must not touch them (a new upload may be running).
    if (ctrl.signal.aborted) return;
    if (results.some((r) => r.status !== "done")) {
      setPhase("failed");
      setError("Some uploads failed. Retry the failed parts, or remove the file and try again.");
      return;
    }
    setPhase("creating");
    try {
      const res = await api.createReview({
        assets: items.map((it, i) => ({ asset_id: results[i].assetId!, role: it.role })),
        brief: { kind: "dub", source_language: src || null, target_languages: tgt ? [tgt] : [] },
      });
      router.push(`/reviews/${encodeURIComponent(res.run_id)}`);
    } catch (err) {
      setPhase("failed");
      setError(`Could not start the review: ${(err as Error).message}`);
    }
  }

  function cancel() {
    abort.current?.abort();
    // Release the server-side multipart uploads of unfinished files (404 is ignored by the client).
    for (const it of [candidate, ...refs])
      if (it?.upload.assetId && it.upload.status !== "done")
        api.abortUpload(it.upload.assetId).catch((e) => console.warn("abort upload failed", e));
    // Cancelled uploads restart from scratch next time (the old multipart upload is abandoned).
    const reset = (it: Item): Item => (it.upload.status === "done" ? it : { ...it, upload: initialUpload(it.file) });
    setCandidate((c) => (c ? reset(c) : c));
    setRefs((rs) => rs.map(reset));
    setPhase("edit");
  }

  const originals = refs.filter((r) => r.role === "original").length;
  const langSelect = (id: string, label: string, value: string, set: (v: string) => void) => (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Select value={value || AUTO} onValueChange={(v) => set(v === AUTO ? "" : v)} disabled={locked}>
        <SelectTrigger id={id} className="w-full bg-field">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={AUTO}>Auto-detect</SelectItem>
          {LANGUAGES.map(([c, n]) => (
            <SelectItem key={c} value={c}>
              {n} ({c})
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );

  return (
    <form onSubmit={submit} className="w-full max-w-[1180px] px-4 py-6 lg:px-6">
      <h1 className="text-2xl font-normal mb-1">New review</h1>
      <p className="text-muted-foreground mb-6">
        Upload the dubbed video and its original. Files go straight to storage; the review starts when all uploads finish.
      </p>

      <div className="grid gap-5 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="space-y-5 min-w-0">
          <Card role="region" aria-labelledby="cand-h">
            <CardHeader>
              <CardTitle id="cand-h" role="heading" aria-level={2}>
                Candidate <span className="font-normal text-muted-foreground">(required)</span>
              </CardTitle>
              <CardDescription>The dubbed video to review</CardDescription>
            </CardHeader>
            <CardContent>
              {candidate ? (
                <ul className="border">
                  <FileRow
                    item={candidate}
                    locked={locked}
                    onRemove={() => {
                      dropPreview(candidate);
                      setCandidate(null);
                    }}
                    onRetry={() => submit()}
                  />
                </ul>
              ) : (
                <DropZone
                  label="choose a file"
                  hint="Video or audio: MP4, MOV, MKV, WebM, WAV…"
                  accept="video/*,audio/*"
                  onFiles={addCandidate}
                />
              )}
            </CardContent>
          </Card>

          <Card role="region" aria-labelledby="refs-h">
            <CardHeader>
              <CardTitle id="refs-h" role="heading" aria-level={2}>
                References
              </CardTitle>
              <CardDescription>The original video, plus optional reference images</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              {refs.length > 0 && (
                <ul className="border divide-y divide-border-subtle">
                  {refs.map((r) => (
                    <FileRow
                      key={r.id}
                      item={r}
                      locked={locked}
                      roleOptions={
                        r.kind === "image"
                          ? [{ value: "reference", label: "Reference image" }]
                          : [
                              { value: "original", label: "Original" },
                              { value: "reference", label: "Reference (unused)" },
                            ]
                      }
                      onRole={(role) => patch(r.id, (x) => ({ ...x, role }))}
                      onRemove={() => {
                        dropPreview(r);
                        setRefs((rs) => rs.filter((x) => x.id !== r.id));
                      }}
                      onRetry={() => submit()}
                    />
                  ))}
                </ul>
              )}
              {originals > 1 && (
                <p className="text-xs text-shouldfix">
                  More than one file is marked Original. The dubbing evaluator compares against one original; mark the others as Reference.
                </p>
              )}
              {!refs.some((r) => r.role === "original") && (
                <p className="text-xs text-muted-foreground">
                  Without an original, the dubbing evaluator can only use the tracks inside the candidate file.
                </p>
              )}
              <DropZone
                compact
                multiple
                label="choose files"
                hint="Original: video or audio. Reference images: PNG, JPEG, WebP."
                accept="video/*,audio/*,image/png,image/jpeg,image/webp"
                onFiles={addRefs}
              />
            </CardContent>
          </Card>
          {rejected.length > 0 && (
            <ErrorBox>
              <ul>
                {rejected.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </ErrorBox>
          )}
        </div>

        <aside className="space-y-4">
          <Card role="region" aria-labelledby="brief-h">
            <CardHeader>
              <CardTitle id="brief-h" role="heading" aria-level={2}>
                Brief
              </CardTitle>
              <CardDescription>Languages of the original and the dub</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              {langSelect(`${formId}-src`, "Source language", src, setSrc)}
              {langSelect(`${formId}-tgt`, "Target language", tgt, setTgt)}
              <p className="text-xs text-muted-foreground">Leave on auto-detect unless detection gets it wrong.</p>
            </CardContent>
          </Card>

          {error && <ErrorBox>{error}</ErrorBox>}

          <div className="flex gap-2">
            {locked ? (
              <>
                <Button type="button" size="lg" disabled className="flex-1">
                  {phase === "creating" ? "Starting review…" : "Uploading…"}
                </Button>
                {phase === "uploading" && (
                  <Button type="button" size="lg" variant="destructive" onClick={cancel}>
                    Cancel
                  </Button>
                )}
              </>
            ) : (
              <Button type="submit" size="lg" disabled={!candidate} className="flex-1">
                <UploadSimpleIcon data-icon="inline-start" />
                {phase === "failed" ? "Retry and start review" : "Upload and start review"}
              </Button>
            )}
          </div>
          {!candidate && <p className="text-xs text-muted-foreground">Add a candidate video to continue.</p>}
        </aside>
      </div>
    </form>
  );
}
