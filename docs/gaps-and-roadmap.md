# Gaps and roadmap

Status as of 2026-10-31, after evaluating two iterations of the 3 Idiots Hindi→Spanish dub (`projects/3-idiots-es`, `projects/3-idiots-es-v2`).

## Observations from the two runs

- v2 changed only the Spanish audio in six regions between 2:19 and 3:48 (about 13% of the track) and reworded two subtitle lines. The video and the bundled Hindi track were byte-identical.
- The deterministic checks gave the same results outside the changed regions (one issue appeared and one disappeared). Inside them, v2 fixed the largest gap (3:02) but introduced early cutoffs at 2:46, 2:51 and 2:56 and a larger early start at 3:27. Score went 10.5 → 9.1, critical issues 18 → 23.
- LLM issues vary between runs of the same input (113 vs 93 total issues on v1), so they can't yet be used for version-to-version comparison.
- The S3 URL was overwritten in place, so v1 now only exists locally.

## LLM dependence

The judge model is `gemini-flash` through OmniRoute (it resolved to `gemini-3.8-flash-tiered` when tested), with `auto/best-fast` as fallback. In the v2 report, 71 of 88 issues came from deterministic checks and 17 from the LLM. A stronger LLM would improve the meaning check (fewer false alarms, steadier results, better English renderings) but would not change any timing result. Transcript quality caps the judge's accuracy more than model tier does, and an audio-input model would add a capability the pipeline doesn't have: judging emotion from the voice.

## Gaps

### 1. Accuracy is unmeasured

- No labelled data, so precision and recall are unknown. Thresholds are hand-picked.
- Timing checks assume the dub sits exactly under the original speech. Good dubs shift words between adjacent lines, so some overruns and early starts are probably acceptable. 16–17% of dub speech falls outside any original speech.
- Non-dialogue sounds (crowd, laughter, applause, singing) can count as original speech that "needs" a dub. This likely inflates missing-dub flags in an auditorium scene.
- LLM results vary between runs; there's no consensus over multiple judge runs, and the report doesn't record which model answered each batch.

### 2. The line unit is wrong

- A line is a chunk of detected speech, not a sentence or subtitle, so one real mistake shows up several times. Example: the dropped "give him a big hand" at 0:25–0:29 appears as both `MISSING_CONTENT` and `EARLY_CUTOFF`.
- Lines are paired by time overlap only. Pairing by meaning would separate "badly timed" from "untranslated".
- There's no grouping of related flags under one root cause.

### 3. Output isn't actionable enough

- Issues are fragments. The dubbing agent needs a per-line instruction: target window, current vs target duration, what to add or cut.
- No built-in version comparison, and issue IDs aren't stable across runs. The v1→v2 diff was a one-off script.
- No per-version storage when a source URL is reused.
- A single score says little. There are no per-dimension scores (timing, sync, meaning, audio), per-line pass/fail, or a ship/don't-ship gate.

### 4. Defects the pipeline can't see

- **Speakers:** no diarization or speaker identity, so a wrong voice on a line, a voice changing across lines, or a gender/age mismatch go undetected.
- **Lip-sync:** the face check measures mouth movement, not which face is speaking or whether mouth shape matches the dubbed audio. It runs on at most 60 windows.
- **TTS quality:** clicks at segment joins, time-stretch artifacts, clipping, overall loudness and voice-vs-background balance are unchecked.
- **Emotion:** judged only from text plus a loudness heuristic.
- **Subtitles:** subtitle-vs-spoken-dub agreement is checked only through the LLM, and subtitle timing against dub speech only for near-empty cues.

### 5. Coverage

Only Hindi→Spanish has run on real files. Other languages are covered by unit tests only. Overlapping speech and songs are untested.

## Roadmap

| # | Item | Why | CPU-ok |
|---|---|---|---|
| 1 | Confirm/false-alarm buttons in the HTML report writing `labels.json`; precision per category; threshold tuning | Makes every later change measurable. Timing labels need no language skills. | yes |
| 2 | Sentence-level lines from subtitle cues / ASR sentences; pair lines by meaning (multilingual embeddings or LLM); group flags by root cause | Biggest reduction in duplicate and false flags | yes |
| 3 | `compare` command with stable line IDs; per-line fix instructions; per-dimension scores and a pass/fail gate; record judge model per issue; store each source version | Closes the loop with the dubbing agent | yes |
| 4 | Speaker diarization and voice-consistency check (speaker embeddings); sound-event filter (YAMNet/PANNs) for crowd/laughter/music | New defect classes; fewer false missing-dub flags | yes, slower |
| 5 | Active-speaker detection and SyncNet-style lip-sync; TTS artifact and loudness checks; audio-input judge for emotion | Visual sync and delivery quality | GPU preferred |
| 6 | Real test files for a non-Latin-script target (e.g. Japanese, Arabic) and a dub with overlapping speech | Validates the multilingual claims | yes |

Suggested start: items 1 and 3 together, since dub iterations are already happening.
