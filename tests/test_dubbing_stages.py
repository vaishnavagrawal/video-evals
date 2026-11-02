from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dubeval.speech import transcribe
from vreview.evaluator import EvalContext
from vreview.evaluators import REGISTRY, DubbingEvaluator
from vreview.evaluators.dubbing.models import DubConfig
from vreview.evaluators.dubbing.stages import assemble, plan_tracks, run_pair, track_features
from vreview.schema import Brief, MediaRef
from vreview.storage.base import media_key, sha256_file
from vreview.storage.local import LocalStore


@pytest.fixture(scope="module")
def sample_media_pair(tmp_path_factory) -> tuple[Path, Path]:
    out_dir = tmp_path_factory.mktemp("sample_pair")
    orig_path = out_dir / "orig.mp4"
    dub_path = out_dir / "dub.mp4"

    orig_src = Path("data/original.mp4")
    dub_src = Path("data/dubbed.mp4")

    if orig_src.exists() and dub_src.exists():
        # Cut 25s window (20s to 45s) where spoken speech and faces exist
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", "20", "-i", str(orig_src), "-t", "25", "-c", "copy", str(orig_path)],
            check=True,
        )
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", "20", "-i", str(dub_src), "-t", "25", "-map", "0", "-c", "copy", str(dub_path)],
            check=True,
        )
    else:
        # Fallback synthetic generation with test tones and visual frame
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24",
                "-f", "lavfi", "-i", "sine=frequency=1000:duration=20",
                "-t", "20", "-pix_fmt", "yuv420p", str(orig_path),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=640x360:rate=24",
                "-f", "lavfi", "-i", "sine=frequency=1200:duration=20",
                "-t", "20", "-pix_fmt", "yuv420p", str(dub_path),
            ],
            check=True,
        )

    return orig_path, dub_path


def test_dubbing_stages_end_to_end(tmp_path, sample_media_pair):
    orig_path, dub_path = sample_media_pair

    store_dir = tmp_path / "store"
    store = LocalStore(store_dir)

    orig_sha = sha256_file(orig_path)
    dub_sha = sha256_file(dub_path)

    orig_key = media_key(orig_sha, ".mp4")
    dub_key = media_key(dub_sha, ".mp4")

    store.put_file(orig_key, orig_path)
    store.put_file(dub_key, dub_path)

    orig_ref = MediaRef(asset_id="orig", role="original", key=orig_key, sha256=orig_sha, filename="orig.mp4")
    dub_ref = MediaRef(asset_id="dub", role="candidate", key=dub_key, sha256=dub_sha, filename="dub.mp4")

    cfg = DubConfig(
        src_lang="en",
        tgt_langs=["es"],
        asr_cloud_model=None,
        asr_local_model="tiny",
        separate=False,
        visual=True,
        llm_models=[],
    )

    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    # 1. plan_tracks
    plan = plan_tracks(store, [orig_ref, dub_ref], cfg, scratch)
    assert plan.original.role == "original"
    assert plan.original.slug == f"{orig_sha[:16]}.a{plan.original.audio_index}"
    assert len(plan.dubs) >= 1
    dub_track = plan.dubs[0]
    assert dub_track.role == "dub"
    assert dub_track.slug == f"{dub_sha[:16]}.a{dub_track.audio_index}"

    # 2. track_features
    src_feat = track_features(store, plan.original, cfg, scratch)
    assert store.exists(src_feat.asr_key)
    dub_feat = track_features(store, dub_track, cfg, scratch)
    assert store.exists(dub_feat.asr_key)

    # Verify second run of track_features is a cache hit (patch speech.transcribe to raise)
    with patch("dubeval.speech.transcribe", side_effect=RuntimeError("transcribe should not be called")):
        cached_src_feat = track_features(store, plan.original, cfg, scratch)
        assert cached_src_feat.asr_key == src_feat.asr_key

    # 3. run_pair
    pair_res = run_pair(store, "run-test-1", src_feat, dub_feat, cfg, scratch)
    assert pair_res.pair == f"{plan.original.lang_tag or 'und'}->{dub_track.lang_tag or 'und'}"
    assert store.exists(pair_res.report_key)
    assert 0.0 <= pair_res.score <= 100.0

    for issue in pair_res.issues:
        assert issue.evaluator == "dubbing"
        assert issue.id.startswith(f"dubbing:{pair_res.pair}:")
        assert 0.0 <= issue.t0 <= issue.t1
        assert issue.track == pair_res.pair

    # 4. assemble
    eval_res = assemble([pair_res], plan)
    assert eval_res.evaluator == "dubbing"
    assert eval_res.version == "1"
    assert eval_res.status == "ok"
    assert eval_res.score == pair_res.score
    assert pair_res.pair in eval_res.artifacts
    assert store.exists(eval_res.artifacts[pair_res.pair])


def test_dubbing_evaluator_contract_and_applies_to(tmp_path, sample_media_pair):
    orig_path, dub_path = sample_media_pair
    store = LocalStore(tmp_path / "store")

    orig_sha = sha256_file(orig_path)
    dub_sha = sha256_file(dub_path)
    orig_key = media_key(orig_sha, ".mp4")
    dub_key = media_key(dub_sha, ".mp4")
    store.put_file(orig_key, orig_path)
    store.put_file(dub_key, dub_path)

    orig_ref = MediaRef(asset_id="orig", role="original", key=orig_key, sha256=orig_sha, filename="orig.mp4")
    dub_ref = MediaRef(asset_id="dub", role="candidate", key=dub_key, sha256=dub_sha, filename="dub.mp4")

    evaluator = REGISTRY["dubbing"]
    assert isinstance(evaluator, DubbingEvaluator)
    assert evaluator.id == "dubbing"
    assert evaluator.version == "1"

    # Brief kind != "dub"
    gen_brief = Brief(kind="generation")
    assert evaluator.applies_to(gen_brief, [orig_ref, dub_ref]) is not None

    # Missing candidate media
    dub_brief = Brief(kind="dub", source_language="en", target_languages=["es"], options={"dubbing": {"asr_cloud_model": None, "asr_local_model": "tiny", "llm_models": []}})
    assert evaluator.applies_to(dub_brief, [orig_ref]) is not None

    # Missing original media
    assert evaluator.applies_to(dub_brief, [dub_ref]) is not None

    # Valid
    assert evaluator.applies_to(dub_brief, [orig_ref, dub_ref]) is None

    # Run via DubbingEvaluator
    ctx = EvalContext(run_id="run-eval-ctx", brief=dub_brief, media=[orig_ref, dub_ref], store=store)
    result = evaluator.run(ctx)
    assert result.status == "ok"
    assert result.score is not None
    assert len(result.artifacts) >= 1
