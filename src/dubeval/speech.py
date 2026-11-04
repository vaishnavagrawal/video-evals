"""Speech activity (Silero VAD via faster-whisper) and ASR (cloud Whisper, local faster-whisper fallback)."""
from __future__ import annotations

import io
import json
import logging
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

log = logging.getLogger("dubeval")

SR = 16000


def load(path: Path) -> np.ndarray:
    x, sr = sf.read(str(path), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SR:
        raise ValueError(f"{path} must be {SR} Hz mono, got {sr}")
    return x


def vad_segments(x: np.ndarray, threshold: float = 0.5, min_silence_ms: int = 250,
                 min_speech_ms: int = 150) -> list[tuple[float, float]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    opts = VadOptions(threshold=threshold, min_silence_duration_ms=min_silence_ms,
                      min_speech_duration_ms=min_speech_ms, speech_pad_ms=30)
    return [(t["start"] / SR, t["end"] / SR) for t in get_speech_timestamps(x, opts)]


def merge(segs: list[tuple[float, float]], gap: float) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for s, e in sorted(segs):
        if out and s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def mask(segs: list[tuple[float, float]], duration: float, hop: float = 0.01) -> np.ndarray:
    m = np.zeros(int(np.ceil(duration / hop)) + 1, bool)
    for s, e in segs:
        m[int(s / hop):int(np.ceil(e / hop))] = True
    return m


# ----------------------------------------------------------------------------------------- ASR


def _to_mp3(wav: Path) -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-i", str(wav), "-ac", "1", "-b:a", "48k", "-f", "mp3", "-"],
                          check=True, capture_output=True).stdout


def _cloud(wav: Path, language: str | None, model: str) -> dict:
    from .providers import RoutedClient

    # Bare model ids go to the default provider; the gateway keeps its own DUBEVAL_ASR_* override.
    client = RoutedClient(timeout=300, max_retries=2, legacy_key_envs=("DUBEVAL_ASR_API_KEY",),
                          legacy_base_url_envs=("DUBEVAL_ASR_BASE_URL",))
    buf = io.BytesIO(_to_mp3(wav))
    buf.name = "audio.mp3"
    kw = {"language": language} if language else {}
    r = client.audio.transcriptions.create(model=model, file=buf, response_format="verbose_json",
                                           timestamp_granularities=["word", "segment"], **kw)
    d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
    segments = [{"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
                for s in d.get("segments") or []]
    words = [{"start": float(w["start"]), "end": float(w["end"]), "word": w["word"].strip()}
             for w in d.get("words") or []]
    if not segments and words:
        segments = _segments_from_words(words)
    if not segments:
        raise RuntimeError(f"ASR model {model} returned no segments or words")
    return {"engine": f"cloud:{model}", "language": d.get("language"), "segments": segments, "words": words}


def _segments_from_words(words: list[dict], gap: float = 0.6, joiner: str = " ") -> list[dict]:
    out: list[dict] = []
    for w in words:
        if out and w["start"] - out[-1]["end"] <= gap:
            out[-1]["end"] = w["end"]
            out[-1]["text"] += joiner + w["word"]
        else:
            out.append({"start": w["start"], "end": w["end"], "text": w["word"]})
    return out


_MODELS: dict[tuple[str, int], object] = {}


def whisper_model(model: str, threads: int):
    from faster_whisper import WhisperModel

    if (model, threads) not in _MODELS:
        _MODELS[(model, threads)] = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)
    return _MODELS[(model, threads)]


def _local(wav: Path, language: str | None, model: str, threads: int) -> dict:
    m = whisper_model(model, threads)
    audio = load(wav)
    segs, info = m.transcribe(audio, language=language, word_timestamps=True, vad_filter=True,
                              beam_size=1, condition_on_previous_text=False)
    segments, words = [], []
    for s in segs:
        segments.append({"start": s.start, "end": s.end, "text": s.text.strip()})
        words += [{"start": w.start, "end": w.end, "word": w.word.strip()} for w in s.words or []]
    return {"engine": f"local:{model}", "language": info.language, "segments": segments, "words": words}


def transcribe(wav: Path, lang, cache: Path, cloud_model: str | None, local_model: str,
               threads: int, warnings: list[str]) -> dict:
    """lang: dubeval.lang.Lang. Languages Whisper doesn't list are transcribed with auto-detection."""
    if cache.exists():
        return json.loads(cache.read_text())
    code = lang.whisper
    if code is None:
        warnings.append(f"{lang.name} ({lang.tag}) is not a Whisper language; ASR runs with auto-detect")
    result = None
    if cloud_model:
        try:
            result = _cloud(wav, code, cloud_model)
        except Exception as e:  # network, quota, auth: fall back to CPU
            warnings.append(f"cloud ASR ({cloud_model}, {lang.tag}) failed, using local {local_model}: {e}")
            log.warning(warnings[-1])
    if result is None:
        result = _local(wav, code, local_model, threads)
    result["joiner"] = lang.joiner
    if not lang.spaced:
        for s in result["segments"]:
            s["text"] = s["text"].replace(" ", "")
    cache.write_text(json.dumps(result, ensure_ascii=False))
    return result


def text_in(asr: dict, start: float, end: float) -> str:
    """Words whose midpoint falls in [start, end); falls back to overlapping segments."""
    j = asr.get("joiner", " ")
    if asr["words"]:
        return j.join(w["word"] for w in asr["words"] if start <= (w["start"] + w["end"]) / 2 < end).strip()
    return j.join(s["text"] for s in asr["segments"] if s["end"] > start and s["start"] < end).strip()
