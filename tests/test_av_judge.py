"""vreview.evaluators.av_judge: windows come from located issues, verdicts fold back, failures are visible."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vreview.evaluators import av_judge as av
from vreview.schema import Brief, EvaluatorResult, Issue, MediaRef, Review
from vreview.storage.local import LocalStore
from vreview.triage import triage


def _issue(i, t0, t1, sev="major", conf=0.8, track="hi->es"):
    return Issue(id=f"dubbing:{track}:I{i:03d}", evaluator="dubbing", category="EARLY_CUTOFF", severity=sev,
                 confidence=conf, t0=t0, t1=t1, track=track, description="cut short", fix_hint="extend",
                 details={"source": "timing"})


def test_windows_merge_pad_and_cap():
    cfg = av.AVJudgeConfig(models=["x"], max_windows=2, pad_s=1, min_window_s=4, max_window_s=12)
    issues = [_issue(1, 10, 11), _issue(2, 12, 13), _issue(3, 40, 41, sev="critical"), _issue(4, 80, 81, "minor")]
    ws = av.plan_windows(issues, 100, cfg)
    assert len(ws) == 2                                    # capped; the minor one is dropped
    assert [i.id[-4:] for i in ws[0].issues] == ["I001", "I002"]
    assert ws[0].t1 - ws[0].t0 <= 12 and ws[0].t0 <= 10 and ws[0].t1 >= 13
    assert ws[1].issues[0].id.endswith("I003")
    # no issues -> evenly spaced samples
    assert len(av.plan_windows([], 30, cfg)) == 2


def test_parse_filters_unknown_ids_and_categories():
    text = json.dumps({"reviews": [{"issue_id": "a", "verdict": "dispute", "confidence": 0.9, "reason": "fine"},
                                   {"issue_id": "zzz", "verdict": "confirm"}],
                       "findings": [{"category": "lip_sync", "severity": "major", "confidence": 0.8, "explanation": "e"},
                                    {"category": "NOPE", "severity": "major"}], "observation": "ok"})
    p = av._parse("```json\n" + text + "```", {"a"})
    assert [r["issue_id"] for r in p["reviews"]] == ["a"]
    assert [f["category"] for f in p["findings"]] == ["LIP_SYNC"]


@pytest.fixture
def media(tmp_path):
    src = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=12",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(src)], check=True)
    store = LocalStore(tmp_path / "blobs")
    store.put_file("media/abc.mp4", src)
    ref = MediaRef(asset_id="a1", role="candidate", key="media/abc.mp4", sha256="abc")
    return store, ref


class FakeClient:
    def __init__(self, handler):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.handler = handler

    def _create(self, **kw):
        self.calls.append(kw)
        out = self.handler(kw)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=out))])


def _reply(kw):
    ids = [i for i in json.dumps(kw["messages"]).split('\\"issue_id\\": \\"')[1:]]
    ids = [x.split('\\"')[0] for x in ids]
    return json.dumps({"reviews": [{"issue_id": i, "verdict": "dispute", "confidence": 0.9, "reason": "looks fine"}
                                   for i in ids if i.startswith("dubbing")],
                       "findings": [{"category": "VOICE_MISMATCH", "severity": "major", "confidence": 0.8,
                                     "explanation": "child voice on adult", "fix_hint": "use adult voice"}],
                       "observation": "ok"})


def test_run_reviews_issues_adds_findings_and_caches(media, tmp_path, monkeypatch):
    store, ref = media
    monkeypatch.setattr(av, "_video_mode", lambda models: "video_url")
    subj = av.Subject(track="hi->es", candidate=av.Stream(media=ref), reference=av.Stream(media=ref, label="orig"))
    issues = [_issue(1, 3, 4), _issue(2, 9, 10)]
    cfg = av.AVJudgeConfig(models=["bad:model", "good"], max_window_s=6)
    calls = {"n": 0}

    def handler(kw):
        calls["n"] += 1
        return RuntimeError("down") if kw["model"] == "bad:model" else _reply(kw)

    client = FakeClient(handler)
    out = av.run(store, Brief(), [subj], issues, cfg, tmp_path / "s1", client=client)
    assert out.result.status == "ok" and not out.result.incomplete
    assert set(out.reviews) == {i.id for i in issues}
    assert all(r["model"] == "good" for r in out.reviews.values())
    assert out.result.issues and out.result.issues[0].category == "VOICE_MISMATCH"
    assert out.result.issues[0].t0 <= 3                    # timestamps from the window, not the model
    video_parts = [p for p in client.calls[-1]["messages"][0]["content"] if p["type"] == "video_url"]
    assert len(video_parts) == 2                           # reference + candidate

    # rerun: cache hit, no calls
    client2 = FakeClient(lambda kw: AssertionError("no call expected"))
    again = av.run(store, Brief(), [subj], issues, cfg, tmp_path / "s2", client=client2)
    assert client2.calls == [] and set(again.reviews) == set(out.reviews)

    # verdicts fold back: disputed -> low confidence -> triage needs-human, tagged
    dub = EvaluatorResult(evaluator="dubbing", version="1", status="ok", issues=issues, score=80)
    av.apply_reviews([dub], out.reviews, cfg)
    assert all(i.confidence <= cfg.dispute_confidence for i in dub.issues)
    from datetime import datetime, timezone
    review = Review(run_id="r", brief=Brief(), media=[ref], results=[dub, out.result], score=80,
                    created_at=datetime.now(timezone.utc))
    tri = triage(review)
    by_id = {i.id: i for i in dub.issues}
    assert "av:disputed" in by_id[issues[0].id].tags and by_id[issues[0].id].priority == "needs-human"


def test_all_failures_mark_incomplete_and_block_approve(media, tmp_path, monkeypatch):
    store, ref = media
    monkeypatch.setattr(av, "_video_mode", lambda models: "video_url")
    subj = av.Subject(candidate=av.Stream(media=ref))
    monkeypatch.setattr(av.time, "sleep", lambda s: None)
    out = av.run(store, Brief(kind="generation"), [subj], [], av.AVJudgeConfig(models=["m"], max_windows=1),
                 tmp_path / "s", client=FakeClient(lambda kw: RuntimeError("429")))
    assert out.result.status == "failed" and out.result.warnings

    from datetime import datetime, timezone
    partial = EvaluatorResult(evaluator="dubbing", version="1", status="ok", score=95,
                              incomplete=["LLM judge failed for lines 1-10 (m: 429)"])
    review = Review(run_id="r", brief=Brief(), media=[ref], results=[partial], score=95,
                    created_at=datetime.now(timezone.utc))
    tri = triage(review)
    assert tri.verdict == "needs_human" and "Incomplete review" in tri.summary


def test_skipped_without_models_or_video_capable_provider(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "b")
    assert av.run(store, Brief(), [], [], av.AVJudgeConfig(), tmp_path).result.status == "skipped"
    monkeypatch.setattr(av, "_video_mode", lambda models: "none")
    assert av.run(store, Brief(), [], [], av.AVJudgeConfig(models=["groq:x"]), tmp_path).result.status == "skipped"
