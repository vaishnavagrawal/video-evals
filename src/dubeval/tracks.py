"""Track discovery for m x n evaluation: which audio stream is the original, which streams are dubs,
and what language each one really is.

Container language tags are unreliable (the 3 Idiots original is tagged `eng` but is Hindi), so every
candidate stream gets Whisper language-ID on its speech. Streams that are a bit-for-bit copy of the
original audio (dub files often carry the source track along) are skipped.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from . import audio, speech
from .lang import Lang, base_of, resolve, same_language
from .media import Probe, extract_audio, probe_cached

log = logging.getLogger("dubeval")

LID_MIN_PROB = 0.5        # below this, a container tag (if any) beats detection
PASSTHROUGH_CORR = 0.9    # waveform correlation above this = same audio as the original


@dataclass
class Track:
    role: str                       # "original" | "dub"
    path: Path
    index: int                      # audio-relative stream index (ffmpeg 0:a:N)
    stream: dict
    dir: Path
    lid: dict                       # {"lang": whisper code|None, "prob": float, "top": [[code, p], ...]}
    lang: Lang | None = None
    lang_source: str = "detected"   # "declared" | "detected" | "tag"
    _x: np.ndarray | None = field(default=None, repr=False)
    _asr: dict | None = field(default=None, repr=False)
    _stems: tuple | None = field(default=None, repr=False)

    @property
    def slug(self) -> str:
        return self.dir.name

    @property
    def wav(self) -> Path:
        return self.dir / "a16k.wav"

    @property
    def x(self) -> np.ndarray:
        if self._x is None:
            self._x = speech.load(self.wav)
        return self._x

    def asr(self, cfg, warnings: list[str]) -> dict:
        if self._asr is None:
            self._asr = speech.transcribe(self.wav, self.lang, self.dir / "asr.json", cfg.asr_cloud_model,
                                          cfg.asr_local_model, cfg.threads, warnings)
        return self._asr

    def stems(self, enabled: bool, threads: int, warnings: list[str]) -> tuple[np.ndarray, np.ndarray, bool]:
        """(vocals, background, separated). Without separation both are the raw mix."""
        if self._stems is None:
            self._stems = (self.x, self.x, False)
            if enabled:
                try:
                    wav44 = extract_audio(self.path, self.index, self.dir / "a44k.wav", 44100)
                    v, b = audio.separate(wav44, self.dir / "sep", threads)
                    self._stems = (speech.load(v), speech.load(b), True)
                except Exception as e:
                    warnings.append(f"demucs separation failed for {self.slug}, using raw mix: {e}")
        return self._stems

    def describe(self) -> dict:
        return {"file": str(self.path), "audio_index": self.index, "container_tag": self.stream.get("lang"),
                "detected": self.lid, "language": None if self.lang is None else self.lang.to_dict(),
                "language_source": self.lang_source}


def _slug(path: Path, index: int) -> str:
    h = hashlib.sha1(str(path.resolve()).encode()).hexdigest()[:6]
    return f"{re.sub(r'[^A-Za-z0-9._-]+', '_', path.stem)[:48]}-{h}.a{index}"


def detect_language(x: np.ndarray, model: str, threads: int, max_speech: float = 120.0) -> dict:
    """Whisper language-ID over the first `max_speech` seconds of VAD speech (music/silence removed)."""
    segs = speech.vad_segments(x)
    chunks, total = [], 0.0
    for s, e in segs:
        chunks.append(x[int(s * speech.SR):int(e * speech.SR)])
        total += e - s
        if total >= max_speech:
            break
    if total < 3.0:
        return {"lang": None, "prob": 0.0, "top": []}
    m = speech.whisper_model(model, threads)
    code, prob, allp = m.detect_language(audio=np.concatenate(chunks), language_detection_segments=4)
    return {"lang": code, "prob": round(float(prob), 3), "top": [[c, round(float(p), 3)] for c, p in allp[:3]]}


def _open(path: Path, index: int, role: str, work: Path, cfg) -> Track:
    p = probe_cached(path)
    d = work / "tracks" / _slug(path, index)
    d.mkdir(parents=True, exist_ok=True)
    extract_audio(path, index, d / "a16k.wav", speech.SR)
    lid_file = d / "lid.json"
    if lid_file.exists():
        lid = json.loads(lid_file.read_text())
    else:
        lid = detect_language(speech.load(d / "a16k.wav"), cfg.asr_local_model, cfg.threads)
        lid_file.write_text(json.dumps(lid))
    return Track(role, path, index, p.audio[index], d, lid)


def effective_lang(stream_tag: str | None, lid: dict) -> str | None:
    """Language code we believe a stream is in: confident detection > container tag > weak detection."""
    if lid.get("lang") and lid.get("prob", 0) >= LID_MIN_PROB:
        return lid["lang"]
    return base_of(stream_tag) or lid.get("lang")


def is_passthrough(a: np.ndarray, b: np.ndarray, window: float = 20.0, max_lag: float = 0.5) -> bool:
    n = min(len(a), len(b))
    w = int(window * speech.SR)
    if n < w:
        w = n
    mid = max(0, n // 2 - w // 2)
    xa, xb = a[mid:mid + w].astype(np.float64), b[mid:mid + w].astype(np.float64)
    denom = np.sqrt((xa ** 2).sum() * (xb ** 2).sum())
    if denom < 1e-9:
        return False
    r = fftconvolve(xa, xb[::-1], "full")
    k, c = int(max_lag * speech.SR), w - 1
    return float(np.abs(r[max(0, c - k):c + k + 1]).max() / denom) >= PASSTHROUGH_CORR


def choose_dubs(cands: list[dict], src: str | None, wanted: list[str]) -> tuple[list[tuple[int, str | None]], list[dict]]:
    """Pure selection rule. cands: [{"index", "lang", "passthrough"}]; wanted: requested base codes.

    Returns ([(index, declared base code or None)], [skipped {"index", "reason"}]).
    - passthrough copies of the original are never evaluated;
    - a lone remaining stream is always evaluated (even if it is in the wrong language: that is a defect);
    - otherwise streams in the source language are skipped and `wanted` filters by language.
    """
    skipped = [{"index": c["index"], "reason": "identical to original audio"} for c in cands if c["passthrough"]]
    rest = [c for c in cands if not c["passthrough"]]
    if len(rest) == 1:
        declared = wanted[0] if len(wanted) == 1 else None
        if len(wanted) > 1 and not any(same_language(rest[0]["lang"], w) for w in wanted):
            skipped.append({"index": rest[0]["index"], "reason": f"language {rest[0]['lang']} not in {wanted}"})
            return [], skipped
        return [(rest[0]["index"], declared)], skipped
    picked: list[tuple[int, str | None]] = []
    for c in rest:
        if same_language(c["lang"], src):
            skipped.append({"index": c["index"], "reason": f"same language as original ({c['lang']})"})
        elif wanted and not any(same_language(c["lang"], w) for w in wanted):
            skipped.append({"index": c["index"], "reason": f"language {c['lang']} not in {wanted}"})
        else:
            picked.append((c["index"], next((w for w in wanted if same_language(c["lang"], w)), None)))
    return picked, skipped


def _assign(t: Track, declared: str | None) -> Track:
    if declared:
        t.lang, t.lang_source = resolve(declared), "declared"
    else:
        code = effective_lang(t.stream.get("lang"), t.lid)
        if code is None:
            raise ValueError(f"cannot determine language of {t.path} audio {t.index}; pass it explicitly")
        t.lang = resolve(code)
        t.lang_source = "detected" if code == t.lid.get("lang") else "tag"
    return t


def original_track(path: Path, cfg, work: Path) -> Track:
    p: Probe = probe_cached(path)
    if not p.audio:
        raise ValueError(f"{path} has no audio stream")
    if cfg.orig_audio is not None:
        idx = cfg.orig_audio
    elif len(p.audio) == 1 or cfg.src_lang is None:
        idx = 0
    else:  # several streams: the one whose detected language matches the declared source
        idx = 0
        for a in p.audio:
            t = _open(path, a["index"], "original", work, cfg)
            if same_language(effective_lang(a.get("lang"), t.lid), resolve(cfg.src_lang).base):
                idx = a["index"]
                break
    return _assign(_open(path, idx, "original", work, cfg), cfg.src_lang)


def dub_tracks(path: Path, src: Track, cfg, work: Path) -> tuple[list[Track], list[dict]]:
    p = probe_cached(path)
    wanted = [resolve(w).tag for w in cfg.tgt_langs]
    by_tag = {resolve(w).base: resolve(w).tag for w in cfg.tgt_langs}
    if cfg.dub_audio is not None:
        t = _open(path, cfg.dub_audio, "dub", work, cfg)
        return [_assign(t, wanted[0] if len(wanted) == 1 else None)], []
    tracks: dict[int, Track] = {}
    cands = []
    for a in p.audio:
        t = _open(path, a["index"], "dub", work, cfg)
        tracks[a["index"]] = t
        cands.append({"index": a["index"], "lang": effective_lang(a.get("lang"), t.lid),
                      "passthrough": is_passthrough(src.x, t.x)})
    picked, skipped = choose_dubs(cands, src.lang.base, [resolve(w).base for w in cfg.tgt_langs])
    for s in skipped:
        s.update(file=str(path), detected=tracks[s["index"]].lid, container_tag=p.audio[s["index"]].get("lang"))
    return [_assign(tracks[i], by_tag.get(d) if d else None) for i, d in picked], skipped
