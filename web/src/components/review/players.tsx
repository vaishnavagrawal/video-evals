"use client";
import type { MediaUrl } from "@/lib/api/types";
import { btn, cx, fmtTime } from "../ui";
import { useSync, type SyncController } from "./sync";

function Speaker({ on }: { on: boolean }) {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden fill="currentColor">
      <path d="M7.563 2.069A.75.75 0 0 1 8 2.75v10.5a.751.751 0 0 1-1.238.57L3.472 11H1.75A1.75 1.75 0 0 1 0 9.25v-2.5C0 5.784.784 5 1.75 5h1.723l3.289-2.82a.75.75 0 0 1 .801-.111Z" />
      {on ? (
        <path d="M10.89 5.11a.75.75 0 0 1 1.06 0 4 4 0 0 1 0 5.66.75.75 0 1 1-1.06-1.06 2.5 2.5 0 0 0 0-3.54.75.75 0 0 1 0-1.06Z" />
      ) : (
        <path d="M10.22 6.22a.75.75 0 0 1 1.06 0L12 6.94l.72-.72a.75.75 0 1 1 1.06 1.06L13.06 8l.72.72a.75.75 0 1 1-1.06 1.06L12 9.06l-.72.72a.75.75 0 1 1-1.06-1.06l.72-.72-.72-.72a.75.75 0 0 1 0-1.06Z" />
      )}
    </svg>
  );
}

function Player({
  label,
  media,
  kind,
  ctl,
}: {
  label: string;
  media: MediaUrl | null | undefined;
  kind: "dub" | "original";
  ctl: SyncController;
}) {
  const audio = useSync(ctl, (s) => s.audio);
  const audible = audio === kind;
  return (
    <figure className="min-w-0">
      <figcaption className="flex items-center gap-2 h-7 text-[12px]">
        <span className="font-semibold text-[13px]">{label}</span>
        <span className="text-fg-muted truncate">{media?.filename ?? ""}</span>
        <button
          type="button"
          onClick={() => ctl.setAudio(kind)}
          aria-pressed={audible}
          className={cx(
            "ml-auto inline-flex items-center gap-1 rounded px-1.5 h-5 border text-[11px]",
            audible ? "border-accent/40 bg-accent-soft text-accent" : "border-line text-fg-muted hover:bg-subtle",
          )}
          title={audible ? `Hearing the ${label.toLowerCase()} audio` : `Switch audio to the ${label.toLowerCase()}`}
        >
          <Speaker on={audible} />
          {audible ? "Audio on" : "Muted"}
        </button>
      </figcaption>
      <div className="relative aspect-video bg-black rounded overflow-hidden">
        {media === undefined ? (
          <div className="absolute inset-0 flex items-center justify-center text-[12px] text-white/60">Loading…</div>
        ) : media === null ? (
          <div className="absolute inset-0 flex items-center justify-center text-[12px] text-white/60">No {label.toLowerCase()} file</div>
        ) : media.kind === "audio" ? (
          <>
            <div className="absolute inset-0 flex items-center justify-center text-[12px] text-white/60">Audio only</div>
            <video ref={(el) => ctl.attach(kind, el)} src={media.url} preload="metadata" playsInline className="hidden" />
          </>
        ) : (
          <video
            ref={(el) => ctl.attach(kind, el)}
            src={media.url}
            preload="metadata"
            playsInline
            onClick={() => ctl.toggle()}
            className="absolute inset-0 size-full object-contain"
            aria-label={`${label} video`}
          />
        )}
      </div>
    </figure>
  );
}

export function Players({
  ctl,
  original,
  dub,
  onPrev,
  onNext,
}: {
  ctl: SyncController;
  original: MediaUrl | null | undefined;
  dub: MediaUrl | null | undefined;
  onPrev: () => void;
  onNext: () => void;
}) {
  return (
    <section aria-label="Players">
      <div className={cx("grid gap-3", original !== null ? "grid-cols-2" : "grid-cols-1")}>
        {original !== null && <Player label="Original" media={original} kind="original" ctl={ctl} />}
        <Player label="Dub" media={dub} kind="dub" ctl={ctl} />
      </div>
      <Transport ctl={ctl} onPrev={onPrev} onNext={onNext} />
    </section>
  );
}

function Transport({ ctl, onPrev, onNext }: { ctl: SyncController; onPrev: () => void; onNext: () => void }) {
  const playing = useSync(ctl, (s) => s.playing);
  const t = useSync(ctl, (s) => Math.round(s.t * 10) / 10);
  const duration = useSync(ctl, (s) => s.duration || ctl.fallbackDuration);
  const rate = useSync(ctl, (s) => s.rate);
  return (
    <div className="flex items-center gap-2 mt-2">
      <button
        type="button"
        onClick={() => ctl.toggle()}
        className={cx(btn.base, btn.default, "w-[72px]")}
        aria-label={playing ? "Pause both videos" : "Play both videos"}
      >
        {playing ? (
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden fill="currentColor">
            <path d="M4 2h3v12H4zM9 2h3v12H9z" />
          </svg>
        ) : (
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden fill="currentColor">
            <path d="M4 2.5v11a.5.5 0 0 0 .77.42l8.5-5.5a.5.5 0 0 0 0-.84l-8.5-5.5A.5.5 0 0 0 4 2.5Z" />
          </svg>
        )}
        {playing ? "Pause" : "Play"}
      </button>
      <button type="button" onClick={onPrev} className={cx(btn.base, btn.default, "px-2")} aria-label="Previous thread (k)" title="Previous thread (k)">
        ‹
      </button>
      <button type="button" onClick={onNext} className={cx(btn.base, btn.default, "px-2")} aria-label="Next thread (j)" title="Next thread (j)">
        ›
      </button>
      <span className="font-mono text-[12px] tabular-nums text-fg-muted whitespace-nowrap">
        {fmtTime(t)} / {fmtTime(duration)}
      </span>
      <input
        type="range"
        min={0}
        max={duration || 0}
        step={0.1}
        value={Math.min(t, duration || 0)}
        onChange={(e) => ctl.seek(Number(e.target.value))}
        aria-label="Seek"
        aria-valuetext={fmtTime(t)}
        className="flex-1 min-w-[80px] accent-[#0969da]"
      />
      <label className="sr-only" htmlFor="rate">
        Playback speed
      </label>
      <select
        id="rate"
        value={rate}
        onChange={(e) => ctl.setRate(Number(e.target.value))}
        className="h-8 rounded-md border border-line bg-canvas px-1.5 text-[12px]"
      >
        {[0.5, 0.75, 1, 1.25, 1.5, 2].map((r) => (
          <option key={r} value={r}>
            {r}×
          </option>
        ))}
      </select>
    </div>
  );
}
