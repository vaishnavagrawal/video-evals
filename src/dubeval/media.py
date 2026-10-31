"""ffmpeg/ffprobe helpers: stream selection, audio extraction, subtitle extraction, frame sampling."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np


def _run(cmd: list[str]) -> bytes:
    return subprocess.run(cmd, check=True, capture_output=True).stdout


@dataclass
class Probe:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
    audio: list[dict]       # [{"index": ord within audio streams, "lang": str|None, ...}]
    subtitles: list[dict]


def probe(path: Path) -> Probe:
    info = json.loads(_run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    num, den = (int(x) for x in video["r_frame_rate"].split("/"))
    audio, subs = [], []
    for s in info["streams"]:
        entry = {"stream_index": s["index"], "lang": s.get("tags", {}).get("language"), "codec": s.get("codec_name")}
        if s["codec_type"] == "audio":
            audio.append({**entry, "index": len(audio)})
        elif s["codec_type"] == "subtitle":
            subs.append({**entry, "index": len(subs)})
    return Probe(path, float(info["format"]["duration"]), int(video["width"]), int(video["height"]),
                 num / den, audio, subs)


def extract_audio(src: Path, audio_index: int, dst: Path, sr: int) -> Path:
    if not dst.exists():
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", f"0:a:{audio_index}",
              "-ac", "1", "-ar", str(sr), str(dst)])
    return dst


def extract_subtitles(src: Path, sub_index: int, dst: Path) -> Path:
    if not dst.exists():
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", f"0:s:{sub_index}", str(dst)])
    return dst


_TC = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(path: Path) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8", errors="replace").strip()):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            m = _TC.search(line)
            if m:
                g = [int(x) for x in m.groups()]
                start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
                end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
                cues.append({"start": start, "end": end, "text": " ".join(lines[i + 1:]).strip()})
                break
    return cues


def sample_frames(src: Path, start: float, end: float, fps: float, width: int) -> np.ndarray:
    """RGB frames [N, H, W, 3] decoded with ffmpeg (fast seek), scaled to `width`."""
    p = probe_cached(src)
    height = int(round(p.height * width / p.width / 2) * 2)
    raw = _run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", str(src),
                "-t", f"{max(0.05, end - start):.3f}", "-vf", f"fps={fps},scale={width}:{height}",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3)


_PROBES: dict[Path, Probe] = {}


def probe_cached(path: Path) -> Probe:
    if path not in _PROBES:
        _PROBES[path] = probe(path)
    return _PROBES[path]


def tc(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
