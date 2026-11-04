"use client";
import { CaretLeftIcon, CaretRightIcon, PauseIcon, PlayIcon } from "@phosphor-icons/react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { MediaUrl } from "@/lib/api/types";
import { cx, fmtTime } from "../common";
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
      <figcaption className="flex items-center gap-2 h-8 text-xs">
        <span className="font-semibold text-sm">{label}</span>
        <span className="text-muted-foreground truncate">{media?.filename ?? ""}</span>
        <Badge asChild variant="outline" className={cx("ml-auto cursor-pointer", audible ? "border-interactive/40 bg-highlight text-link" : "text-muted-foreground hover:bg-accent")}>
          <button
            type="button"
            onClick={() => ctl.setAudio(kind)}
            aria-pressed={audible}
            title={audible ? `Hearing the ${label.toLowerCase()} audio` : `Switch audio to the ${label.toLowerCase()}`}
          >
            <Speaker on={audible} />
            {audible ? "Audio on" : "Muted"}
          </button>
        </Badge>
      </figcaption>
      <div className="relative aspect-video bg-media overflow-hidden">
        {media === undefined ? (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-media-foreground">Loading…</div>
        ) : media === null ? (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-media-foreground">No {label.toLowerCase()} file</div>
        ) : media.kind === "audio" ? (
          <>
            <div className="absolute inset-0 flex items-center justify-center text-xs text-media-foreground">Audio only</div>
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
    <Card aria-label="Players" role="region" className="gap-0 p-3">
      <div className={cx("grid gap-3", original !== null ? "grid-cols-2" : "grid-cols-1")}>
        {original !== null && <Player label="Original" media={original} kind="original" ctl={ctl} />}
        <Player label="Dub" media={dub} kind="dub" ctl={ctl} />
      </div>
      <Transport ctl={ctl} onPrev={onPrev} onNext={onNext} />
    </Card>
  );
}

function Transport({ ctl, onPrev, onNext }: { ctl: SyncController; onPrev: () => void; onNext: () => void }) {
  const playing = useSync(ctl, (s) => s.playing);
  const t = useSync(ctl, (s) => Math.round(s.t * 10) / 10);
  const duration = useSync(ctl, (s) => s.duration || ctl.fallbackDuration);
  const rate = useSync(ctl, (s) => s.rate);
  return (
    <div className="flex items-center gap-2 mt-3">
      <Button
        onClick={() => ctl.toggle()}
        className="w-[84px]"
        aria-label={playing ? "Pause both videos" : "Play both videos"}
      >
        {playing ? <PauseIcon weight="fill" data-icon="inline-start" /> : <PlayIcon weight="fill" data-icon="inline-start" />}
        {playing ? "Pause" : "Play"}
      </Button>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="outline" size="icon" onClick={onPrev} aria-label="Previous thread (k)">
            <CaretLeftIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Previous thread (k)</TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button variant="outline" size="icon" onClick={onNext} aria-label="Next thread (j)">
            <CaretRightIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent>Next thread (j)</TooltipContent>
      </Tooltip>
      <span className="font-mono text-xs tabular-nums text-muted-foreground whitespace-nowrap">
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
        className="flex-1 min-w-[80px] accent-interactive"
      />
      <Select value={String(rate)} onValueChange={(v) => ctl.setRate(Number(v))}>
        <SelectTrigger className="w-[76px]" aria-label="Playback speed">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {[0.5, 0.75, 1, 1.25, 1.5, 2].map((r) => (
            <SelectItem key={r} value={String(r)}>
              {r}×
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
