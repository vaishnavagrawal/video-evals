# Submission draft (Meta Global AI Developer Hackathon, Indie track)

**Project:** vreview: code review for dubbed and AI-generated video
**Track / theme:** Indie developer · Build for Your World
**Repo:** https://github.com/vaishnavagrawal/video-evals
**Live demo:** URL and judge login are provided in the submission form (sign-up is closed)
**Demo video:** https://drive.google.com/file/d/1czXDR0c8n_BgiNQk4AEeC3OAW8K5r281/view?usp=sharing

## One-liner
vreview reviews a dubbed or AI-generated video the way a senior engineer reviews a pull request: timestamped
threads with a priority, evidence and a fix instruction, from a pipeline where Meta's Muse Spark watches and
listens to the video and deterministic signal processing keeps it honest.

## Problem
AI dubbing and video generation now produce more footage than people can check. A dub can cut off while the
actor is still talking, lose a joke, or play over a closed mouth, and nobody who reviews it speaks both
languages. Teams either ship unchecked or pay for slow manual QA.

## What it does
- Upload the original and the dub (or call REST, MCP or a webhook). The review returns a verdict
  (`request_changes`, `needs_human`, `approve`), a score, and threads grouped as blocker, should-fix,
  needs-human and nit, each with a fix instruction a dubbing or generation system can act on.
- A review page plays the original and the dub side by side, with an issue timeline and accept /
  false-positive feedback that is stored as future evaluation data.
- A live pipeline view shows every stage, sub-run, model used and cache hit.

## How Meta's models are used
- **Muse Spark as an audio-video judge.** The pipeline cuts short clips of the original and the dub around
  every issue the deterministic checks located and sends them to Muse Spark as video with audio. It confirms or
  disputes each issue and reports what signal processing can't see: lip sync, a voice that doesn't fit the
  speaker, lost emotion, TTS artifacts.
- **Muse Spark as a text judge**, in an ensemble with Gemini Flash, for meaning, missing content and tone.
- **Muse Spark as the review writer** for every thread comment and the summary.

Design rule: timestamps only come from deterministic layers (VAD, alignment, ASR word times, face analysis).
The models judge evidence that is already located, so they can't invent a time. A disputed issue keeps its
time but drops to needs-human.

## Results (live deployment, 60 s Hindi→Spanish dub)
- Full review in 4 min 42 s: verdict `request_changes`, 15 findings in 11 threads.
- Muse Spark watched 8 clips: it confirmed 4 issues, disputed 8 and found 1 new lip-sync problem. Several
  disputes were "missing dub" flags over audience laughter, a known false-positive class of the timing checks.
- 525 backend tests pass.

## Technical highlights
Hatchet DAG with child runs per audio track and per dub, content-hash caching per stage, FastAPI + FastMCP,
better-auth JWTs, presigned multipart uploads, Next.js review UI, deployed on Coolify. Every model is a
`provider:model` config value: swap Muse Spark, Gemini or a self-hosted Llama without code changes.

## Honest limits
Accuracy is unmeasured (no labelled set yet; reviewer feedback is stored to build one). Model findings vary
between runs. Only Hindi→Spanish has been tested on real footage. The demo footage is a short excerpt of a
commercial film, used only to demonstrate QA.
