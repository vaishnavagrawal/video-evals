from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from dubeval import audio, speech
from dubeval import pipeline as dubeval_pipeline
from dubeval.lang import resolve
from dubeval.media import extract_audio, probe
from dubeval.tracks import Track, choose_dubs, detect_language, effective_lang, is_passthrough
from vreview.evaluator import EvalContext
from vreview.schema import Brief, EvaluatorResult, Issue, MediaRef
from vreview.storage.base import BlobStore, cache_prefix, review_key

from .models import DubConfig, PairResult, TrackFeatures, TrackPlan, TrackRef


def plan_tracks(store: BlobStore, media: list[MediaRef], cfg: DubConfig, scratch: Path) -> TrackPlan:
    # Reference media (e.g. images) is stored with the review but dubbing never reads it. Dropping it here
    # also keeps it out of the plan cache key, so attaching a reference doesn't invalidate the cached plan.
    media = [m for m in media if m.role != "reference"]
    plan_cfg = {
        "src_lang": cfg.src_lang,
        "tgt_langs": sorted(cfg.tgt_langs),
        "orig_audio": cfg.orig_audio,
        "dub_audio": cfg.dub_audio,
        "asr_local_model": cfg.asr_local_model,
    }
    composite_sha = hashlib.sha256(
        ",".join(f"{m.role}:{m.sha256}" for m in sorted(media, key=lambda m: (m.role, m.sha256))).encode()
    ).hexdigest()
    plan_key = f"{cache_prefix(composite_sha, 'plan', '1', plan_cfg)}/plan.json"

    if store.exists(plan_key):
        return TrackPlan.model_validate_json(store.get_bytes(plan_key))

    orig_media = next((m for m in media if m.role == "original"), None)
    if orig_media is None:
        raise ValueError("No media ref with role 'original' found")
    cand_medias = [m for m in media if m.role == "candidate"]
    if not cand_medias:
        raise ValueError("At least one candidate media ref is required")

    media_dir = scratch / "media"
    wavs_dir = scratch / "wavs"
    media_dir.mkdir(parents=True, exist_ok=True)
    wavs_dir.mkdir(parents=True, exist_ok=True)

    orig_path = media_dir / f"{orig_media.sha256}.mp4"
    store.get_file(orig_media.key, orig_path)
    op = probe(orig_path)
    if not op.audio:
        raise ValueError(f"Original media {orig_media.key} has no audio stream")

    if cfg.orig_audio is not None:
        orig_audio_index = cfg.orig_audio
    elif len(op.audio) == 1 or cfg.src_lang is None:
        orig_audio_index = 0
    else:
        orig_audio_index = 0
        wanted_base = resolve(cfg.src_lang).base
        for a in op.audio:
            idx = a["index"]
            a16_key = f"{cache_prefix(orig_media.sha256, f'audio.a{idx}', '1', {})}/a16k.wav"
            if not store.exists(a16_key):
                w_path = wavs_dir / f"{orig_media.sha256}.a{idx}.wav"
                extract_audio(orig_path, idx, w_path, speech.SR)
                store.put_file(a16_key, w_path, content_type="audio/wav")
            else:
                w_path = store.get_file(a16_key, wavs_dir / f"{orig_media.sha256}.a{idx}.wav")

            l_key = f"{cache_prefix(orig_media.sha256, f'lid.a{idx}', '1', {'model': cfg.asr_local_model})}/lid.json"
            if store.exists(l_key):
                a_lid = json.loads(store.get_bytes(l_key))
            else:
                a_x = speech.load(w_path)
                a_lid = detect_language(a_x, cfg.asr_local_model, cfg.threads)
                store.put_bytes(l_key, json.dumps(a_lid).encode("utf-8"), content_type="application/json")

            if effective_lang(a.get("lang"), a_lid) == wanted_base:
                orig_audio_index = idx
                break

    orig_wav_key = f"{cache_prefix(orig_media.sha256, f'audio.a{orig_audio_index}', '1', {})}/a16k.wav"
    orig_wav_path = wavs_dir / f"{orig_media.sha256}.a{orig_audio_index}.wav"
    if not store.exists(orig_wav_key):
        extract_audio(orig_path, orig_audio_index, orig_wav_path, speech.SR)
        store.put_file(orig_wav_key, orig_wav_path, content_type="audio/wav")
    else:
        store.get_file(orig_wav_key, orig_wav_path)

    orig_lid_key = f"{cache_prefix(orig_media.sha256, f'lid.a{orig_audio_index}', '1', {'model': cfg.asr_local_model})}/lid.json"
    if store.exists(orig_lid_key):
        orig_lid = json.loads(store.get_bytes(orig_lid_key))
    else:
        orig_x_full = speech.load(orig_wav_path)
        orig_lid = detect_language(orig_x_full, cfg.asr_local_model, cfg.threads)
        store.put_bytes(orig_lid_key, json.dumps(orig_lid).encode("utf-8"), content_type="application/json")

    orig_stream = op.audio[orig_audio_index]
    if cfg.src_lang:
        orig_lang = resolve(cfg.src_lang)
        orig_lang_tag = orig_lang.tag
        orig_lang_source = "declared"
    else:
        code = effective_lang(orig_stream.get("lang"), orig_lid)
        if code is None:
            raise ValueError(f"Cannot determine language of original {orig_media.key} stream {orig_audio_index}")
        orig_lang = resolve(code)
        orig_lang_tag = orig_lang.tag
        orig_lang_source = "detected" if code == orig_lid.get("lang") else "tag"

    orig_slug = f"{orig_media.sha256[:16]}.a{orig_audio_index}"
    orig_ref = TrackRef(
        media=orig_media,
        audio_index=orig_audio_index,
        role="original",
        slug=orig_slug,
        lang_tag=orig_lang_tag,
        lang_source=orig_lang_source,
        lid=orig_lid,
        stream=orig_stream,
        wav_key=orig_wav_key,
    )

    orig_x = speech.load(orig_wav_path)
    dubs: list[TrackRef] = []
    skipped: list[dict] = []
    warnings: list[str] = []

    wanted = [resolve(w).tag for w in cfg.tgt_langs]
    by_tag = {resolve(w).base: resolve(w).tag for w in cfg.tgt_langs}
    wanted_bases = [resolve(w).base for w in cfg.tgt_langs]

    for cand_media in cand_medias:
        cand_path = media_dir / f"{cand_media.sha256}.mp4"
        store.get_file(cand_media.key, cand_path)
        cp = probe(cand_path)
        if not cp.audio:
            warnings.append(f"Candidate media {cand_media.key} has no audio stream")
            continue

        if cfg.dub_audio is not None:
            idx = cfg.dub_audio
            cand_wav_key = f"{cache_prefix(cand_media.sha256, f'audio.a{idx}', '1', {})}/a16k.wav"
            cand_wav_path = wavs_dir / f"{cand_media.sha256}.a{idx}.wav"
            if not store.exists(cand_wav_key):
                extract_audio(cand_path, idx, cand_wav_path, speech.SR)
                store.put_file(cand_wav_key, cand_wav_path, content_type="audio/wav")
            else:
                store.get_file(cand_wav_key, cand_wav_path)

            cand_lid_key = f"{cache_prefix(cand_media.sha256, f'lid.a{idx}', '1', {'model': cfg.asr_local_model})}/lid.json"
            if store.exists(cand_lid_key):
                cand_lid = json.loads(store.get_bytes(cand_lid_key))
            else:
                cand_x = speech.load(cand_wav_path)
                cand_lid = detect_language(cand_x, cfg.asr_local_model, cfg.threads)
                store.put_bytes(cand_lid_key, json.dumps(cand_lid).encode("utf-8"), content_type="application/json")

            declared = wanted[0] if len(wanted) == 1 else None
            if declared:
                dub_lang = resolve(declared)
                dub_lang_source = "declared"
            else:
                code = effective_lang(cp.audio[idx].get("lang"), cand_lid)
                if code is None:
                    raise ValueError(f"Cannot determine language of candidate {cand_media.key} audio {idx}")
                dub_lang = resolve(code)
                dub_lang_source = "detected" if code == cand_lid.get("lang") else "tag"

            dubs.append(
                TrackRef(
                    media=cand_media,
                    audio_index=idx,
                    role="dub",
                    slug=f"{cand_media.sha256[:16]}.a{idx}",
                    lang_tag=dub_lang.tag,
                    lang_source=dub_lang_source,
                    lid=cand_lid,
                    stream=cp.audio[idx],
                    wav_key=cand_wav_key,
                )
            )
            continue

        cand_info = {}
        cands = []
        for a in cp.audio:
            idx = a["index"]
            cand_wav_key = f"{cache_prefix(cand_media.sha256, f'audio.a{idx}', '1', {})}/a16k.wav"
            cand_wav_path = wavs_dir / f"{cand_media.sha256}.a{idx}.wav"
            if not store.exists(cand_wav_key):
                extract_audio(cand_path, idx, cand_wav_path, speech.SR)
                store.put_file(cand_wav_key, cand_wav_path, content_type="audio/wav")
            else:
                store.get_file(cand_wav_key, cand_wav_path)

            cand_lid_key = f"{cache_prefix(cand_media.sha256, f'lid.a{idx}', '1', {'model': cfg.asr_local_model})}/lid.json"
            if store.exists(cand_lid_key):
                cand_lid = json.loads(store.get_bytes(cand_lid_key))
            else:
                cand_x = speech.load(cand_wav_path)
                cand_lid = detect_language(cand_x, cfg.asr_local_model, cfg.threads)
                store.put_bytes(cand_lid_key, json.dumps(cand_lid).encode("utf-8"), content_type="application/json")

            cand_x = speech.load(cand_wav_path)
            pt = is_passthrough(orig_x, cand_x)
            eff_code = effective_lang(a.get("lang"), cand_lid)
            cands.append({"index": idx, "lang": eff_code, "passthrough": pt})
            cand_info[idx] = {"stream": a, "lid": cand_lid, "wav_key": cand_wav_key}

        picked, sk = choose_dubs(cands, orig_lang.base, wanted_bases)
        for s in sk:
            s.update(
                file=cand_media.key,
                detected=cand_info[s["index"]]["lid"],
                container_tag=cand_info[s["index"]]["stream"].get("lang"),
            )
            skipped.append(s)

        for i, declared in picked:
            info = cand_info[i]
            tag = by_tag.get(declared) if declared else None
            if tag:
                dub_lang = resolve(tag)
                dub_lang_source = "declared"
            else:
                code = effective_lang(info["stream"].get("lang"), info["lid"])
                dub_lang = resolve(code) if code else None
                dub_lang_source = "detected" if (code and code == info["lid"].get("lang")) else "tag"

            dubs.append(
                TrackRef(
                    media=cand_media,
                    audio_index=i,
                    role="dub",
                    slug=f"{cand_media.sha256[:16]}.a{i}",
                    lang_tag=dub_lang.tag if dub_lang else None,
                    lang_source=dub_lang_source,
                    lid=info["lid"],
                    stream=info["stream"],
                    wav_key=info["wav_key"],
                )
            )

    plan = TrackPlan(original=orig_ref, dubs=dubs, skipped=skipped, warnings=warnings)
    store.put_bytes(plan_key, plan.model_dump_json().encode("utf-8"), content_type="application/json")
    return plan


def track_features(store: BlobStore, track: TrackRef, cfg: DubConfig, scratch: Path) -> TrackFeatures:
    asr_cfg = {
        "cloud_model": cfg.asr_cloud_model,
        "local_model": cfg.asr_local_model,
        "lang": track.lang_tag,
    }
    asr_key = f"{cache_prefix(track.media.sha256, f'asr.a{track.audio_index}', '1', asr_cfg)}/asr.json"

    stems_cfg = {"threads": cfg.threads} if cfg.separate else None
    feat_cfg = {
        "asr": asr_cfg,
        "separate": cfg.separate,
        "stems": stems_cfg,
    }
    feat_key = f"{cache_prefix(track.media.sha256, f'features.a{track.audio_index}', '1', feat_cfg)}/features.json"

    if store.exists(feat_key):
        return TrackFeatures.model_validate_json(store.get_bytes(feat_key))

    warnings: list[str] = []

    # 1. ASR
    if not store.exists(asr_key):
        feat_dir = scratch / "features"
        feat_dir.mkdir(parents=True, exist_ok=True)
        wav_path = feat_dir / f"{track.slug}.wav"
        store.get_file(track.wav_key, wav_path)
        lang = resolve(track.lang_tag) if track.lang_tag else None
        asr_scratch_json = feat_dir / f"{track.slug}.asr.json"
        asr_res = speech.transcribe(
            wav=wav_path,
            lang=lang,
            cache=asr_scratch_json,
            cloud_model=cfg.asr_cloud_model,
            local_model=cfg.asr_local_model,
            threads=cfg.threads,
            warnings=warnings,
        )
        store.put_bytes(asr_key, json.dumps(asr_res, ensure_ascii=False).encode("utf-8"), content_type="application/json")

    # 2. Stems
    vocals_key: str | None = None
    bed_key: str | None = None
    if cfg.separate:
        stems_prefix = cache_prefix(track.media.sha256, f"stems.a{track.audio_index}", "1", stems_cfg or {})
        vocals_key = f"{stems_prefix}/vocals16k.wav"
        bed_key = f"{stems_prefix}/bed16k.wav"
        if not (store.exists(vocals_key) and store.exists(bed_key)):
            try:
                media_dir = scratch / "media"
                media_dir.mkdir(parents=True, exist_ok=True)
                media_path = media_dir / f"{track.media.sha256}.mp4"
                store.get_file(track.media.key, media_path)
                sep_dir = scratch / "sep" / track.slug
                sep_dir.mkdir(parents=True, exist_ok=True)
                wav44 = extract_audio(media_path, track.audio_index, sep_dir / "a44k.wav", 44100)
                v_path, b_path = audio.separate(wav44, sep_dir, cfg.threads)
                v16 = sep_dir / "vocals16k.wav"
                b16 = sep_dir / "bed16k.wav"
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", str(v_path), "-ac", "1", "-ar", "16000", str(v16)],
                    check=True,
                )
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", str(b_path), "-ac", "1", "-ar", "16000", str(b16)],
                    check=True,
                )
                store.put_file(vocals_key, v16, content_type="audio/wav")
                store.put_file(bed_key, b16, content_type="audio/wav")
            except Exception as e:
                warnings.append(f"Demucs separation failed for {track.slug}: {e}")
                vocals_key = None
                bed_key = None

    features = TrackFeatures(
        track=track,
        asr_key=asr_key,
        vocals_key=vocals_key,
        bed_key=bed_key,
        warnings=warnings,
    )
    store.put_bytes(feat_key, features.model_dump_json().encode("utf-8"), content_type="application/json")
    return features


def run_pair(
    store: BlobStore,
    run_id: str,
    src: TrackFeatures,
    dub: TrackFeatures,
    cfg: DubConfig,
    scratch: Path,
) -> PairResult:
    src_tag = src.track.lang_tag or "src"
    dub_tag = dub.track.lang_tag or "dub"
    pair = f"{src_tag}->{dub_tag}"
    pair_slug = pair

    rep_key = review_key(run_id, f"dubbing/{pair_slug}.json")

    pair_sha = hashlib.sha256(
        f"{src.track.media.sha256}:{src.track.audio_index}:{dub.track.media.sha256}:{dub.track.audio_index}".encode()
    ).hexdigest()
    pair_cfg = {
        "llm_models": sorted(cfg.llm_models),
        **({"judge_mode": cfg.judge_mode} if cfg.judge_mode != "fallback" else {}),   # old cache keys stay valid
        "visual": cfg.visual,
        "max_visual_windows": cfg.max_visual_windows,
        "separate": cfg.separate,
    }
    pair_cache_key = f"{cache_prefix(pair_sha, 'pair_result', '1', pair_cfg)}/pair_result.json"

    if store.exists(pair_cache_key):
        pair_res = PairResult.model_validate_json(store.get_bytes(pair_cache_key))
        if not store.exists(rep_key):
            if store.exists(pair_res.report_key):
                store.copy(pair_res.report_key, rep_key)
            else:
                store.put_bytes(rep_key, store.get_bytes(pair_cache_key), content_type="application/json")
        pair_res.report_key = rep_key
        return pair_res

    pair_scratch = scratch / "run_pair" / f"{src.track.slug}__{dub.track.slug}"
    pair_scratch.mkdir(parents=True, exist_ok=True)

    media_dir = scratch / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    src_media_path = media_dir / f"{src.track.media.sha256}.mp4"
    dub_media_path = media_dir / f"{dub.track.media.sha256}.mp4"
    store.get_file(src.track.media.key, src_media_path)
    store.get_file(dub.track.media.key, dub_media_path)

    wavs_dir = scratch / "wavs"
    wavs_dir.mkdir(parents=True, exist_ok=True)
    src_wav_path = wavs_dir / f"{src.track.slug}.wav"
    dub_wav_path = wavs_dir / f"{dub.track.slug}.wav"
    store.get_file(src.track.wav_key, src_wav_path)
    store.get_file(dub.track.wav_key, dub_wav_path)

    src_x = speech.load(src_wav_path)
    dub_x = speech.load(dub_wav_path)

    asr_o = json.loads(store.get_bytes(src.asr_key))
    asr_d = json.loads(store.get_bytes(dub.asr_key))

    src_stems = None
    dub_stems = None
    if src.vocals_key and src.bed_key and dub.vocals_key and dub.bed_key:
        try:
            stems_dir = scratch / "stems"
            stems_dir.mkdir(parents=True, exist_ok=True)
            ov_path = store.get_file(src.vocals_key, stems_dir / f"{src.track.slug}.vocals.wav")
            ob_path = store.get_file(src.bed_key, stems_dir / f"{src.track.slug}.bed.wav")
            dv_path = store.get_file(dub.vocals_key, stems_dir / f"{dub.track.slug}.vocals.wav")
            db_path = store.get_file(dub.bed_key, stems_dir / f"{dub.track.slug}.bed.wav")
            src_stems = (speech.load(ov_path), speech.load(ob_path), True)
            dub_stems = (speech.load(dv_path), speech.load(db_path), True)
        except Exception:
            src_stems = None
            dub_stems = None

    src_track_dir = scratch / "tracks" / src.track.slug
    src_track_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_wav_path, src_track_dir / "a16k.wav")
    (src_track_dir / "asr.json").write_text(json.dumps(asr_o, ensure_ascii=False))

    dub_track_dir = scratch / "tracks" / dub.track.slug
    dub_track_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dub_wav_path, dub_track_dir / "a16k.wav")
    (dub_track_dir / "asr.json").write_text(json.dumps(asr_d, ensure_ascii=False))

    src_lang_obj = resolve(src.track.lang_tag) if src.track.lang_tag else resolve("und")
    dub_lang_obj = resolve(dub.track.lang_tag) if dub.track.lang_tag else resolve("und")

    dubeval_src = Track(
        role=src.track.role,
        path=src_media_path,
        index=src.track.audio_index,
        stream=src.track.stream,
        dir=src_track_dir,
        lid=src.track.lid,
        lang=src_lang_obj,
        lang_source=src.track.lang_source,
        _x=src_x,
        _asr=asr_o,
        _stems=src_stems,
    )
    dubeval_dub = Track(
        role=dub.track.role,
        path=dub_media_path,
        index=dub.track.audio_index,
        stream=dub.track.stream,
        dir=dub_track_dir,
        lid=dub.track.lid,
        lang=dub_lang_obj,
        lang_source=dub.track.lang_source,
        _x=dub_x,
        _asr=asr_d,
        _stems=dub_stems,
    )

    dcfg = dubeval_pipeline.Config(
        src_lang=cfg.src_lang,
        tgt_langs=cfg.tgt_langs,
        orig_audio=cfg.orig_audio,
        dub_audio=cfg.dub_audio,
        asr_cloud_model=cfg.asr_cloud_model,
        asr_local_model=cfg.asr_local_model,
        llm_models=cfg.llm_models,
        judge_mode=cfg.judge_mode,
        separate=cfg.separate,
        visual=cfg.visual,
        max_visual_windows=cfg.max_visual_windows,
        threads=cfg.threads,
    )

    report = dubeval_pipeline.run_pair(dubeval_src, dubeval_dub, pair_scratch, dcfg)
    report_bytes = report.model_dump_json().encode("utf-8")
    store.put_bytes(rep_key, report_bytes, content_type="application/json")

    v_issues = []
    for old in report.issues:
        vis = old.visual.model_dump() if getattr(old, "visual", None) else None
        v_issues.append(
            Issue(
                id=f"dubbing:{report.pair}:{old.id}",
                evaluator="dubbing",
                category=old.category,
                severity=old.severity,
                confidence=old.confidence,
                t0=old.start,
                t1=old.end,
                track=report.pair,
                description=old.description,
                fix_hint=old.fix_hint,
                regenerate_scope="segment",
                details={
                    "source": old.source,
                    "original_text": old.original_text,
                    "dubbed_text": old.dubbed_text,
                    "evidence": old.evidence,
                    "visual": vis,
                },
            )
        )

    pair_result = PairResult(
        pair=report.pair,
        report_key=rep_key,
        score=report.summary.score,
        issues=v_issues,
        summary=report.summary.model_dump(),
        warnings=report.warnings,
        timings=report.stage_timings,
    )
    store.put_bytes(pair_cache_key, pair_result.model_dump_json().encode("utf-8"), content_type="application/json")
    return pair_result


def assemble(pairs: list[PairResult], plan: TrackPlan) -> EvaluatorResult:
    if not pairs:
        return EvaluatorResult(
            evaluator="dubbing",
            version="1",
            status="skipped",
            reason="No candidate dub pairs were evaluated",
            warnings=list(plan.warnings),
        )
    score = min(p.score for p in pairs)
    all_issues = [iss for p in pairs for iss in p.issues]
    summary = {p.pair: p.summary for p in pairs}
    artifacts = {p.pair: p.report_key for p in pairs}
    warnings = list(plan.warnings) + [w for p in pairs for w in p.warnings]
    timings: dict[str, float] = {}
    for p in pairs:
        for k, v in p.timings.items():
            timings[k] = timings.get(k, 0.0) + v

    # dubeval.judge reports a chunk that failed on every model as a warning and drops its findings.
    incomplete = [w for w in warnings if w.startswith("LLM judge failed")]

    return EvaluatorResult(
        evaluator="dubbing",
        version="1",
        status="ok",
        incomplete=incomplete,
        score=score,
        issues=all_issues,
        summary=summary,
        artifacts=artifacts,
        warnings=warnings,
        timings=timings,
    )


class DubbingEvaluator:
    id: str = "dubbing"
    version: str = "1"

    def applies_to(self, brief: Brief, media: list[MediaRef]) -> str | None:
        if brief.kind != "dub":
            return f"dubbing evaluator does not apply to brief kind {brief.kind!r}"
        originals = [m for m in media if m.role == "original"]
        candidates = [m for m in media if m.role == "candidate"]
        if len(originals) != 1:
            return f"dubbing requires exactly 1 original media, found {len(originals)}"
        if len(candidates) < 1:
            return f"dubbing requires at least 1 candidate media, found {len(candidates)}"
        return None

    def run(self, ctx: EvalContext) -> EvaluatorResult:
        reason = self.applies_to(ctx.brief, ctx.media)
        if reason is not None:
            return EvaluatorResult(
                evaluator=self.id,
                version=self.version,
                status="skipped",
                reason=reason,
            )
        cfg = DubConfig.from_brief(ctx.brief)
        with tempfile.TemporaryDirectory(prefix=f"vreview_dubbing_{ctx.run_id}_") as tmpdir:
            scratch = Path(tmpdir)
            plan = plan_tracks(ctx.store, ctx.media, cfg, scratch)
            src_feat = track_features(ctx.store, plan.original, cfg, scratch)
            pairs: list[PairResult] = []
            for dub_track in plan.dubs:
                dub_feat = track_features(ctx.store, dub_track, cfg, scratch)
                p_res = run_pair(ctx.store, ctx.run_id, src_feat, dub_feat, cfg, scratch)
                pairs.append(p_res)
            return assemble(pairs, plan)
