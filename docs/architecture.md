# dubeval architecture

dubeval compares an original video with one or more dubbed versions and reports where the dub is off: timing gaps, lag, missing lines, meaning changes, wrong language, background audio loss. The output is structured JSON meant to be fed back to the agent that generated the dub, plus an HTML page for human review.

It runs on CPU. Remote services are used only for speech-to-text and the LLM judge, both through an OpenAI-compatible endpoint, and both have local or disable switches.

## Design principles

- **Timestamps come from signal processing, never from the LLM.** The LLM only judges line pairs that the timing layer has already aligned, and its issues inherit those times.
- **The original and the dub share one video timeline.** A correct dub puts speech where the original actor speaks, so no global audio alignment is needed.
- **Nothing is language-specific.** Languages are resolved or detected per audio track; every layer reads the same language record.
- **Every expensive step is cached per track.** An original compared against n dubs is processed once, and reruns only redo what changed or failed.
- **Files, not a database.** Projects, caches, reports and run history are plain folders and JSON.

## Pipeline

```
inputs: original video, dub video(s)
  │
  ├─ tracks      probe streams → extract 16 kHz mono → Whisper language-ID per stream
  │              pick original stream; pick dub streams (skip copies of the original,
  │              skip source-language streams unless it is the only one)
  │
  └─ per (original track, dub track) pair
       stems      optional Demucs vocals/background split
       vad_align  Silero VAD on both → merge original segments into lines
                  → assign each dub segment to the line it overlaps most
       asr        Whisper (cloud, local faster-whisper fallback), word timestamps
       detect     timing issues, background dropout, energy mismatch, mix offset,
                  track-level wrong/untranslated language
       subtitles  dub's own subtitle stream in the dub language: cue vs speech coverage
       judge      LLM over aligned (original text, dub text, subtitle text) per line
       visual     frame identity check; MediaPipe mouth activity on flagged windows
                  → raise/lower severity
       finalize   dedupe overlapping same-category issues, ids, aligned_lines, score
  │
  outputs: BatchReport JSON → HTML visualizer; project run history + index
```

## Modules (`src/dubeval/`)

| Module | Role |
|---|---|
| `lang.py` | Resolves any label (BCP-47 tag, ISO 639 code, English name) into one `Lang` record: tag, base code, display name, Whisper code, whether words are space-separated. Groups languages Whisper can't tell apart (hi/ur, sr/hr/bs, ms/id, no/nb/nn/da, zh/yue). |
| `media.py` | ffprobe/ffmpeg wrappers: probe, audio extraction, subtitle extraction and SRT parsing, frame sampling, timecode formatting. |
| `tracks.py` | `Track` objects with cached audio, language-ID, ASR and stems. Picks the original stream and the dub streams; detects passthrough copies by waveform correlation. |
| `speech.py` | Silero VAD (via faster-whisper), segment merging and masks, cloud and local ASR, text lookup by time window with the right word joiner. |
| `timing.py` | Line alignment and all timing detectors; summary stats (overlap, unmatched ratio, onset median/p90). |
| `audio.py` | Demucs separation, RMS envelopes, background-bed comparison and offset, energy-mismatch detection. |
| `visual.py` | Sparse frame comparison (was the picture changed?) and MediaPipe FaceMesh mouth-opening variance on flagged windows; severity adjustment rules. |
| `judge.py` | Language-neutral LLM prompt, batching, model fallback list, per-batch cache keyed by input hash. |
| `pipeline.py` | `Config`, `run_pair` (one language pair) and `run` (one original, many dub files). |
| `schema.py` | Pydantic output contract (`dubeval.v2`). |
| `viz.py` | Renders a report into one self-contained HTML file. |
| `projects.py` | File-based multi-video projects: add sources (path, URL, Google Drive), run, history, index page. |
| `__init__.py` | `dubeval` CLI. |

## Detectors

Timing (from VAD on both tracks; defaults in `timing.Thresholds`):

| Category | Rule (defaults) |
|---|---|
| `MISSING_DUB` | dub covers < 25% of an original line ≥ 0.8 s |
| `LATE_START` / `EARLY_START` | dub onset > +0.5 s / < −0.6 s from the original line |
| `EARLY_CUTOFF` | dub ends > 0.7 s before the original line ends |
| `OVERRUN` | dub ends > 0.8 s after the original line (notes if it spills into the next line) |
| `MID_LINE_GAP` | ≥ 0.8 s of dub silence while the original speaks, inside a line |
| `UNMATCHED_SPEECH` | ≥ 0.6 s of dub speech with no original line within 1 s |
| `SYNC_DRIFT` | rolling median onset over 5 lines ≥ 0.4 s; also whole-mix background offset > 40 ms |
| `FAST_SPEECH` | dub chars/s > 1.6× the dub's own median (needs ASR words) |

Audio: `BACKGROUND_DROPOUT` (dub bed ≥ 12 dB quieter for ≥ 1 s, on non-speech frames or on stems) and `ENERGY_MISMATCH` (original line loud by z-score, dub flat).

Language: `UNTRANSLATED` (the dub track is detected as the source language) and `WRONG_LANGUAGE` (it is detected as some other language). Either one, when critical, sets the score to 0.

Semantic (LLM): `MEANING_MISMATCH`, `MISSING_CONTENT`, `ADDED_CONTENT`, `TONE_MISMATCH`, `NAME_OR_TERM_ERROR`, `UNTRANSLATED`, `SCRIPT_DEVIATION`.

Other: `SUBTITLE_AUDIO_DESYNC` (cue with < 15% dub speech), `VIDEO_MISMATCH`, `DURATION_MISMATCH`.

Visual adjustment: when a usable face is moving its mouth (mouth-ratio std ≥ 0.02) during a gap, severity goes up; when there is no usable face, timing severities go down one step.

## Output contract (`dubeval.v2`)

- `BatchReport`: `original`, `reports[]`, `skipped_tracks[]`, `failed[]`.
- `Report` (one language pair):
  - identity: `pair` (e.g. `hi->es`), `source`/`target` track descriptions (file, stream, container tag, detected and declared language)
  - `summary`: speech seconds, overlap, unmatched ratio, onset median/p90, background offset, counts, score
  - `issues[]`: id, category, source, severity, confidence, start/end with timecodes, description, original/dub text, fix hint, evidence, visual evidence
  - `aligned_lines[]`: every original line with where the dub placed it, onset, coverage, both texts, both English renderings, linked issue ids
  - `stage_timings`, `warnings`

Score: `100 · exp(−Σ weight·confidence / max(1, minutes·15))`, with weights critical 6, major 2.5, minor 0.7. It is a rough indicator, not a calibrated metric.

## Language handling

- `--src-lang` / `--tgt-lang` are optional; when omitted, each stream's language comes from Whisper language-ID on its first 120 s of speech.
- Confident detection (p ≥ 0.5) overrides the container tag. That case is real: the 3 Idiots original is tagged `eng` and is Hindi.
- Scripts without spaces (Han, Japanese, Thai, Lao, Khmer, Myanmar, Tibetan) join words with no space.
- Languages Whisper doesn't list run ASR in auto-detect mode, with a warning.

## Caching and file layout

```
projects/
  index.html                     all projects, latest score per pair
  <name>/
    project.json                 sources, languages, run history
    media/original.*, dub-NN.*   downloads or symlinks
    work/tracks/<file-hash>.aN/  a16k.wav, lid.json, asr.json, a44k.wav, sep/
    work/pairs/<src>__<dub>/     subtitles, judge_cache.json
    report.json, report.html     latest run
    runs/<UTC timestamp>/report.json
```

## Entry points

```bash
uv run dubeval ORIGINAL DUB [DUB ...] [--src-lang hi] [--tgt-lang es-419,pt-BR] [--separate] -o report.json
uv run dubeval-viz report.json -o report.html
uv run dubeval-project add NAME --original SRC --dub SRC [--dub SRC] [--run] [--separate]
uv run dubeval-project run NAME|all
uv run dubeval-project list | index
```

Endpoint configuration comes from the environment: `DUBEVAL_ASR_*` / `DUBEVAL_LLM_*` (`_API_KEY`, `_BASE_URL`), falling back to `OMNIROUTE_*`, then `OPENAI_*`. The defaults are ASR `groq/whisper-large-v3` and judge `gemini-flash` with fallback `auto/best-fast`.

## Cost on CPU (5-minute video, 8 cores)

| Stage | Time |
|---|---|
| Download | ~1 min per 460 MB |
| Demucs (optional) | ~3 min per track |
| VAD + detectors | < 10 s |
| ASR (cloud) | ~20 s per track |
| Language-ID (local `small`) | a few seconds per track |
| LLM judge | 4–11 min, dominated by endpoint latency and rate limits |
| Face check | ~40 s |

## Viewing reports

The HTML seeks video by byte range. Serve the folder with a range-capable server, for example `uvx --from rangehttpserver==1.4.0 python -m RangeHTTPServer`; Python's plain `http.server` does not support ranges.

## Moving to GPU

Swap points that don't change the contract: local Whisper large for ASR and language-ID, Demucs on GPU, TalkNet/SyncNet in place of the mouth-variance check, and speaker embeddings for voice identity.
