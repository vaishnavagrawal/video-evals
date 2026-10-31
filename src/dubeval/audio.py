"""Audio layer: background-bed integrity and delivery-energy mismatch.

Works on the raw mixes by default. If Demucs stems are available (`--separate`), vocals are used for
energy comparison and the accompaniment stems for the background check, which removes the speech bias.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from .speech import SR, load, mask

log = logging.getLogger("dubeval")
HOP = 0.01


def separate(wav44: Path, out_dir: Path, threads: int) -> tuple[Path, Path]:
    """htdemucs two-stem split on CPU; returns (vocals, no_vocals) as 16 kHz mono wavs."""
    stem = out_dir / "htdemucs" / wav44.stem
    vocals16, bed16 = out_dir / f"{wav44.stem}.vocals.wav", out_dir / f"{wav44.stem}.bed.wav"
    if not (vocals16.exists() and bed16.exists()):
        if not (stem / "vocals.wav").exists():
            subprocess.run([sys.executable, "-m", "demucs", "-n", "htdemucs", "--two-stems", "vocals",
                            "-j", str(threads), "-o", str(out_dir), str(wav44)], check=True, capture_output=True)
        for src, dst in ((stem / "vocals.wav", vocals16), (stem / "no_vocals.wav", bed16)):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", str(SR), str(dst)],
                           check=True)
    return vocals16, bed16


def rms_db(x: np.ndarray, win: float = 0.05) -> np.ndarray:
    """dBFS envelope at HOP resolution."""
    n, h = int(win * SR), int(HOP * SR)
    frames = len(x) // h
    sq = np.convolve(x.astype(np.float64) ** 2, np.ones(n) / n, mode="same")[: frames * h: h]
    return 10 * np.log10(sq + 1e-10)


def best_offset(a: np.ndarray, b: np.ndarray, max_shift: float = 2.0) -> tuple[float, float]:
    """Lag (s) of b relative to a and normalised correlation peak, on downsampled envelopes."""
    ea, eb = a - a.mean(), b - b.mean()
    r = fftconvolve(ea, eb[::-1], "full")
    mid = len(eb) - 1
    k = int(max_shift / HOP)
    win = r[mid - k: mid + k + 1]
    i = int(win.argmax())
    peak = win[i] / (np.sqrt((ea ** 2).sum() * (eb ** 2).sum()) + 1e-9)
    return (i - k) * HOP, float(peak)


def background_check(orig_bed: np.ndarray, dub_bed: np.ndarray, orig_segs, dub_segs, duration: float,
                     using_stems: bool, min_len: float = 1.0, drop_db: float = 12.0) -> tuple[list[dict], float | None]:
    """Flag windows where the dub's non-speech bed is much quieter than the original's.

    Only frames where neither track has speech are compared, so speech loudness differences don't count
    (unless stems are used, in which case every frame is a fair comparison).
    """
    eo, ed = rms_db(orig_bed), rms_db(dub_bed)
    n = min(len(eo), len(ed))
    eo, ed = eo[:n], ed[:n]
    offset, peak = best_offset(eo[: min(n, 6000)], ed[: min(n, 6000)])
    if using_stems:
        valid = np.ones(n, bool)
    else:
        valid = ~(mask(orig_segs, duration, HOP)[:n] | mask(dub_segs, duration, HOP)[:n])
    audible = eo > -50  # original bed has something worth preserving
    drop = valid & audible & ((eo - ed) > drop_db)
    # smooth: 0.5 s majority
    k = 50
    drop = np.convolve(drop.astype(float), np.ones(k) / k, mode="same") > 0.5
    issues = []
    d = np.diff(np.concatenate([[0], drop.astype(np.int8), [0]]))
    for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
        if (b - a) * HOP < min_len:
            continue
        delta = float(np.median((eo - ed)[a:b][valid[a:b]])) if valid[a:b].any() else float(np.median((eo - ed)[a:b]))
        issues.append({
            "category": "BACKGROUND_DROPOUT", "source": "audio",
            "severity": "major" if delta > 20 or (b - a) * HOP > 3 else "minor",
            "confidence": 0.7 if using_stems else 0.55, "start": round(a * HOP, 2), "end": round(b * HOP, 2),
            "description": f"Background music/effects are ~{delta:.0f} dB quieter in the dub for {(b - a) * HOP:.1f}s.",
            "fix_hint": "Re-mix using the original background stem for this window (ducking or stem loss).",
            "evidence": {"median_db_drop": round(delta, 1), "stems": using_stems},
        })
    return issues, round(offset * 1000, 1) if peak > 0.3 else None


def energy_mismatch(lines, orig_voc: np.ndarray, dub_voc: np.ndarray, z: float = 1.3) -> list[dict]:
    """A line the original delivers loudly (relative to the actor track's norm) but the dub delivers flat."""
    eo, ed = rms_db(orig_voc), rms_db(dub_voc)

    def level(env, s, e):
        a, b = int(s / HOP), int(np.ceil(e / HOP))
        seg = env[a:b]
        return float(np.percentile(seg, 90)) if len(seg) else np.nan

    rows = [(ln, level(eo, ln.start, ln.end), level(ed, ln.dub_start, ln.dub_end))
            for ln in lines if ln.dub and ln.coverage >= 0.4 and ln.dur >= 0.8]
    if len(rows) < 8:
        return []
    o = np.array([r[1] for r in rows])
    d = np.array([r[2] for r in rows])
    zo = (o - np.nanmedian(o)) / (np.nanstd(o) + 1e-6)
    zd = (d - np.nanmedian(d)) / (np.nanstd(d) + 1e-6)
    issues = []
    for (ln, _, _), a, b in zip(rows, zo, zd):
        if a - b >= 2 * z and a >= z:
            issues.append({
                "category": "ENERGY_MISMATCH", "source": "audio", "severity": "minor", "confidence": 0.5,
                "start": round(ln.start, 2), "end": round(ln.end, 2), "line_index": ln.index,
                "description": f"Original line is delivered loudly (z={a:+.1f}) but the dub is flat (z={b:+.1f}).",
                "fix_hint": "Regenerate with a higher-intensity/emotional voice style (shouting/excited).",
                "evidence": {"orig_level_z": round(float(a), 2), "dub_level_z": round(float(b), 2)},
            })
    return issues


