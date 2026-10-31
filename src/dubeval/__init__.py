"""dubeval: find timing gaps, lag and translation defects in dubbed videos vs their original.

Language agnostic: source and target languages are detected per audio stream (Whisper LID) unless
declared. One original can be compared against many dub files, each with many audio streams.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _table(report) -> str:
    s = report.summary
    out = [f"== {report.pair}  [{report.target['file']} a:{report.target['audio_index']}]",
           f"score {s.score}/100 | issues {s.severity_counts} | overlap {s.speech_overlap_ratio:.0%} | "
           f"median onset {s.median_onset_delta}s | p90 |onset| {s.p90_abs_onset_delta}s",
           f"{'id':5} {'start':>12} {'end':>12} {'sev':8} {'category':22} description"]
    for x in report.issues:
        out.append(f"{x.id:5} {x.start_tc:>12} {x.end_tc:>12} {x.severity:8} {x.category:22} {x.description}")
    return "\n".join(out)


def _list(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    from .lang import resolve
    from .pipeline import Config, run

    p = argparse.ArgumentParser(prog="dubeval", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("original", type=Path)
    p.add_argument("dubbed", type=Path, nargs="+", help="one or more dubbed videos")
    p.add_argument("-o", "--out", type=Path, default=Path("report.json"))
    p.add_argument("--work", type=Path, help="cache dir (default: <out>.work)")
    p.add_argument("--src-lang", help="original language (BCP-47/ISO/name); default: detect")
    p.add_argument("--tgt-lang", help="comma-separated dub languages to evaluate, e.g. 'es-419,pt-BR'; "
                                      "default: every dub stream not in the source language (detected)")
    p.add_argument("--orig-audio", type=int, help="audio stream index in the original")
    p.add_argument("--dub-audio", type=int, help="audio stream index in each dub (disables auto-selection)")
    p.add_argument("--asr-model", default="groq/whisper-large-v3", help="cloud ASR model; 'none' = local only")
    p.add_argument("--local-asr-model", default="small", help="faster-whisper model (LID + CPU ASR fallback)")
    p.add_argument("--llm-model", default="gemini-flash,auto/best-fast",
                   help="comma-separated judge models tried in order; 'none' disables")
    p.add_argument("--separate", action="store_true", help="Demucs vocal/bed split (CPU: ~3 min per 5 min audio)")
    p.add_argument("--no-visual", action="store_true")
    p.add_argument("--threads", type=int, default=4)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    for noisy in ("httpx", "httpcore", "openai", "huggingface_hub", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    tgt = _list(a.tgt_lang)
    try:  # fail fast on unknown labels before any heavy work
        for label in ([a.src_lang] if a.src_lang else []) + tgt:
            resolve(label)
    except ValueError as e:
        p.error(str(e))
    llm = [] if (a.llm_model or "").lower() == "none" else _list(a.llm_model)
    cfg = Config(src_lang=a.src_lang, tgt_langs=tgt, orig_audio=a.orig_audio, dub_audio=a.dub_audio,
                 asr_cloud_model=None if a.asr_model.lower() == "none" else a.asr_model,
                 asr_local_model=a.local_asr_model, llm_models=llm, separate=a.separate,
                 visual=not a.no_visual, threads=a.threads)
    batch = run(a.original, a.dubbed, a.work or a.out.with_suffix(".work"), cfg)
    a.out.write_text(batch.model_dump_json(indent=2))
    for r in batch.reports:
        print(_table(r), end="\n\n")
        for w in r.warnings:
            print(f"warning [{r.pair}]:", w, file=sys.stderr)
    for s in batch.skipped_tracks:
        print(f"skipped {s['file']} a:{s['index']}: {s['reason']}", file=sys.stderr)
    for f in batch.failed:
        print(f"FAILED {f['dub']}: {f['error']}", file=sys.stderr)
    print(f"wrote {a.out} ({len(batch.reports)} pair(s))", file=sys.stderr)
    return 1 if batch.failed and not batch.reports else 0
