"""Visual layer: on flagged windows only, check whether a visible face is actually talking.

MediaPipe FaceMesh (CPU) -> per-face mouth-opening ratio (inner-lip gap / face height). A talking face
shows high variance in that ratio. Used to raise/lower severity of timing issues:
  - no face / tiny face  -> off-screen speaker, timing error is less visible
  - large face, talking  -> lip-sync error is very visible
Also verifies the two videos are frame-identical (the dub must not alter the picture).
"""
from __future__ import annotations
import hashlib
from pathlib import Path

import numpy as np

from .media import sample_frames

UPPER_LIP, LOWER_LIP, FOREHEAD, CHIN = 13, 14, 10, 152
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"
MODEL_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"


def ensure_model() -> Path:
    cache_dir = Path.home() / ".cache" / "dubeval"
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_path = cache_dir / "face_landmarker.task"
    if model_path.exists():
        h = hashlib.sha256(model_path.read_bytes()).hexdigest()
        if h == MODEL_SHA256:
            return model_path
    import urllib.request
    tmp = model_path.with_suffix(".tmp")
    req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "dubeval/1.0"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(64 * 1024):
            f.write(chunk)
    observed = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if observed != MODEL_SHA256:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"face_landmarker.task sha256 mismatch: got {observed}, expected {MODEL_SHA256}")
    tmp.replace(model_path)
    return model_path


class MouthAnalyzer:
    def __init__(self, max_faces: int = 4):
        from mediapipe.tasks.python import BaseOptions, vision

        model_path = ensure_model()
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=max_faces,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.landmarker = vision.FaceLandmarker.create_from_options(options)
        self._timestamp_ms = 0
    def analyze(self, video, start: float, end: float, fps: float = 8.0, width: int = 640) -> dict:
        frames = sample_frames(video, start, end, fps, width)
        tracks: dict[int, list[float]] = {}
        areas: dict[int, float] = {}
        face_frames = 0
        import mediapipe as mp

        step_ms = max(1, int(round(1000.0 / fps)))
        for f in frames:
            self._timestamp_ms += step_ms
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=f)
            res = self.landmarker.detect_for_video(mp_image, timestamp_ms=self._timestamp_ms)
            if not res.face_landmarks:
                continue
            face_frames += 1
            # stable ordering by horizontal position so a face keeps its slot across frames
            faces = sorted(res.face_landmarks, key=lambda m: m[1].x)
            for slot, p in enumerate(faces):
                xs = np.array([q.x for q in p])
                ys = np.array([q.y for q in p])
                face_h = abs(p[CHIN].y - p[FOREHEAD].y) + 1e-6
                tracks.setdefault(slot, []).append(abs(p[LOWER_LIP].y - p[UPPER_LIP].y) / face_h)
                areas[slot] = max(areas.get(slot, 0.0), float((xs.max() - xs.min()) * (ys.max() - ys.min())))
        activity = {s: float(np.std(v)) for s, v in tracks.items() if len(v) >= 3}
        best = max(activity, key=activity.get) if activity else None
        mouth = activity.get(best, 0.0) if best is not None else 0.0
        max_area = max(areas.values()) if areas else 0.0
        usable = best is not None and max_area >= 0.004
        return {
            "frames_analyzed": int(len(frames)),
            "face_frames": face_frames,
            "max_face_area": round(max_area, 4),
            "mouth_activity": round(mouth, 4),
            "speaking_likely": (mouth >= 0.02) if usable else None,
        }

    def close(self) -> None:
        if getattr(self, "landmarker", None) is not None:
            self.landmarker.close()
            self.landmarker = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def video_identity(orig, dub, duration: float, n: int = 12, width: int = 160) -> list[dict]:
    """Compare sparse frames; any large pixel difference means the picture was changed or shifted."""
    issues = []
    for t in np.linspace(1.0, max(1.0, duration - 1.0), n):
        a = sample_frames(orig, float(t), float(t) + 0.04, 25, width)
        b = sample_frames(dub, float(t), float(t) + 0.04, 25, width)
        if not len(a) or not len(b):
            continue
        diff = float(np.abs(a[0].astype(int) - b[0].astype(int)).mean())
        if diff > 12:
            issues.append({
                "category": "VIDEO_MISMATCH", "source": "video", "severity": "major", "confidence": 0.7,
                "start": round(float(t), 2), "end": round(float(t) + 0.04, 2),
                "description": f"Video frame differs from the original (mean abs pixel diff {diff:.1f}).",
                "fix_hint": "Dub must reuse the original video stream untouched (stream copy); check re-encode/offset.",
                "evidence": {"mean_abs_diff": round(diff, 1)},
            })
    return issues


def adjust(issue: dict, vis: dict) -> None:
    """Re-weight a timing issue with visual evidence (in place)."""
    order = ["minor", "major", "critical"]
    sev = order.index(issue["severity"])
    cat = issue["category"]
    talking = vis["speaking_likely"]
    if talking is None:
        if cat in ("EARLY_CUTOFF", "MID_LINE_GAP", "LATE_START", "OVERRUN", "EARLY_START"):
            sev = max(0, sev - 1)
            issue["description"] += " No clear face on screen (likely off-screen speaker), so less visible."
    elif talking and cat in ("EARLY_CUTOFF", "MID_LINE_GAP", "LATE_START", "MISSING_DUB"):
        if vis["max_face_area"] >= 0.02:
            sev = min(2, sev + 1)
        issue["description"] += " A visible face is moving its mouth during this window (lip-sync break)."
        issue["confidence"] = round(min(0.98, issue["confidence"] + 0.1), 2)
    elif talking is False and cat in ("OVERRUN", "UNMATCHED_SPEECH"):
        sev = min(2, sev + 1)
        issue["description"] += " On-screen face is not talking while dubbed voice plays."
    issue["severity"] = order[sev]
