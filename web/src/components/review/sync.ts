"use client";
import { useSyncExternalStore } from "react";

export type AudioSource = "dub" | "original";

interface State {
  t: number;
  playing: boolean;
  duration: number;
  rate: number;
  audio: AudioSource;
}

/**
 * One transport for two <video> elements. The dub (candidate) is the clock; the original follows and is
 * nudged back when it drifts more than 0.15 s. Outside React so the 60 fps time updates only re-render
 * the components that subscribe to `t`.
 */
export class SyncController {
  private dub: HTMLVideoElement | null = null;
  private orig: HTMLVideoElement | null = null;
  private state: State = { t: 0, playing: false, duration: 0, rate: 1, audio: "dub" };
  private subs = new Set<() => void>();
  private raf = 0;
  private lastFix = 0;
  private wired = new WeakSet<HTMLVideoElement>();
  fallbackDuration = 0;

  /** Used until (or instead of) video metadata: the evaluator's reported duration. */
  setFallbackDuration(d: number) {
    this.fallbackDuration = d;
    this.set({ duration: this.durationOf() });
  }

  subscribe = (fn: () => void) => {
    this.subs.add(fn);
    return () => this.subs.delete(fn);
  };
  get = () => this.state;
  private set(p: Partial<State>) {
    this.state = { ...this.state, ...p };
    this.subs.forEach((f) => f());
  }

  private get master() {
    return this.dub ?? this.orig;
  }
  private get all() {
    return [this.dub, this.orig].filter(Boolean) as HTMLVideoElement[];
  }

  attach(kind: "dub" | "original", el: HTMLVideoElement | null) {
    if (kind === "dub") this.dub = el;
    else this.orig = el;
    if (el) {
      el.playbackRate = this.state.rate;
      this.applyAudio();
      if (this.wired.has(el)) return;
      this.wired.add(el);
      const onMeta = () => this.set({ duration: this.durationOf() });
      el.addEventListener("loadedmetadata", onMeta);
      el.addEventListener("ended", () => {
        if (el === this.master) this.pause();
      });
      if (el.readyState >= 1) onMeta();
    }
  }

  private durationOf() {
    const d = this.master?.duration;
    return d && Number.isFinite(d) ? d : this.fallbackDuration;
  }

  private loop = () => {
    const m = this.master;
    if (!m) return;
    const now = performance.now();
    if (this.dub && this.orig && now - this.lastFix > 500 && Math.abs(this.orig.currentTime - this.dub.currentTime) > 0.15) {
      this.orig.currentTime = this.dub.currentTime;
      this.lastFix = now;
    }
    this.set({ t: m.currentTime });
    if (this.state.playing) this.raf = requestAnimationFrame(this.loop);
  };

  async play() {
    if (!this.master) return;
    const t = this.master.currentTime;
    for (const v of this.all) if (Math.abs(v.currentTime - t) > 0.05) v.currentTime = t;
    this.set({ playing: true });
    cancelAnimationFrame(this.raf);
    this.raf = requestAnimationFrame(this.loop);
    try {
      await Promise.all(this.all.map((v) => v.play()));
    } catch {
      this.pause();
    }
  }
  pause() {
    this.all.forEach((v) => v.pause());
    cancelAnimationFrame(this.raf);
    this.set({ playing: false, t: this.master?.currentTime ?? this.state.t });
  }
  toggle() {
    if (this.state.playing) this.pause();
    else void this.play();
  }
  seek(t: number) {
    const d = this.state.duration || this.fallbackDuration || Infinity;
    const tt = Math.max(0, Math.min(t, d));
    this.all.forEach((v) => {
      v.currentTime = tt;
    });
    this.set({ t: tt });
  }
  nudge(dt: number) {
    this.seek(this.state.t + dt);
  }
  setRate(rate: number) {
    this.all.forEach((v) => (v.playbackRate = rate));
    this.set({ rate });
  }
  setAudio(audio: AudioSource) {
    this.set({ audio });
    this.applyAudio();
  }
  private applyAudio() {
    if (this.dub) this.dub.muted = this.state.audio !== "dub" && !!this.orig;
    if (this.orig) this.orig.muted = this.state.audio !== "original" && !!this.dub;
  }
  destroy() {
    cancelAnimationFrame(this.raf);
    this.all.forEach((v) => v.pause());
  }
}

export function useSync<T>(ctl: SyncController, sel: (s: State) => T): T {
  return useSyncExternalStore(
    ctl.subscribe,
    () => sel(ctl.get()),
    () => sel(ctl.get()),
  );
}
