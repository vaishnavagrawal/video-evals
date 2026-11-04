"""Semantic layer: LLM judge over time-aligned (original ASR, dubbed ASR, dub subtitle) triples.

The LLM never computes timestamps; it only judges line pairs that the timing layer already aligned,
so every semantic issue inherits exact times from the deterministic layer.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("dubeval")

PROMPT = """You are a QA reviewer for film dubbing from {src} into {tgt}. The human reviewer speaks
neither language, so you are their only check on meaning. Each item is one original utterance with:
`orig` (ASR of the original {src} audio), `dub` (ASR of the dubbed {tgt} audio placed over the same
time window), and optionally `script` (the dub's own subtitle text for that window). ASR may contain
small spelling errors and phonetic name misspellings; do not report those. Neighbouring items may
legitimately shift a few words across a boundary; only report content loss/addition that is not
covered by the adjacent items. Judge against natural, idiomatic {tgt}, not a literal translation.

For EVERY item return an English rendering of `orig` and of `dub` (copy as-is if already English).
Then report only real problems:
- MEANING_MISMATCH: dub says something different from orig
- MISSING_CONTENT: a meaningful part of orig is absent in dub (and not in neighbouring items)
- ADDED_CONTENT: dub adds meaning not in orig
- TONE_MISMATCH: register/emotion clearly different (e.g. insult vs polite, joke lost)
- NAME_OR_TERM_ERROR: names/terms changed or wrong
- UNTRANSLATED: dub is still in {src} or in a language other than {tgt}
- SCRIPT_DEVIATION: spoken dub clearly differs in meaning from `script`
Severity: critical = plot/joke broken or wrong language; major = noticeable meaning loss; minor = nuance.

Return JSON: {{"items":[{{"i":<index>,"orig_en":"...","dub_en":"...","issues":[{{"category":"...",
"severity":"critical|major|minor","confidence":0.0-1.0,"explanation":"English, one sentence",
"fix_hint":"concrete instruction for the dubbing system"}}]}}]}}

Items:
{items}"""

CATS = {"MEANING_MISMATCH", "MISSING_CONTENT", "ADDED_CONTENT", "TONE_MISMATCH", "NAME_OR_TERM_ERROR",
        "UNTRANSLATED", "SCRIPT_DEVIATION"}


def _client():
    """Routes each model reference to its provider (dubeval.providers); legacy DUBEVAL_LLM_* still work."""
    from .providers import RoutedClient

    return RoutedClient(timeout=150, max_retries=0)


def _chunk(client, model: str, items: list[dict], src: str, tgt: str) -> list[dict]:
    content = PROMPT.format(src=src, tgt=tgt, items=json.dumps(items, ensure_ascii=False, indent=0))
    r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": content}],
                                       response_format={"type": "json_object"}, temperature=0)
    text = (r.choices[0].message.content or "{}").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
    items_out = json.loads(text).get("items", [])
    if not items_out:
        raise ValueError("empty items")
    return items_out


def judge(rows: list[dict], models: list[str], src: str, tgt: str, cache: Path, warnings: list[str],
          chunk: int = 10, workers: int = 3) -> dict[int, dict]:
    """rows: [{"i", "orig", "dub", "script"}]. Returns {i: {"orig_en", "dub_en", "issues"}}.

    Each chunk tries every model in order (2 attempts each); results are cached per input hash so a
    rerun only re-asks chunks that failed or changed.
    """
    client = _client()
    cached: dict = json.loads(cache.read_text()) if cache.exists() else {}
    chunks = [rows[k:k + chunk] for k in range(0, len(rows), chunk)]

    def key(c):
        return hashlib.sha1(json.dumps(c, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def run(c):
        k = key(c)
        if k in cached:
            return k, cached[k]
        last = None
        for model in models:
            for attempt in range(2):
                try:
                    res = _chunk(client, model, c, src, tgt)
                    for item in res:
                        if isinstance(item, dict):
                            item["model"] = model      # provenance: which model judged this line
                    return k, res
                except Exception as e:  # timeout / 429 / bad JSON -> retry, then next model
                    last = f"{model}: {str(e)[:160]}"
                    time.sleep(3 * (attempt + 1))
        warnings.append(f"LLM judge failed for lines {c[0]['i']}-{c[-1]['i']} ({last})")
        log.warning(warnings[-1])
        return k, None

    out: dict[int, dict] = {}
    with ThreadPoolExecutor(workers) as ex:
        for k, res in ex.map(run, chunks):
            if res is None:
                continue
            cached[k] = res
            for item in res:
                if isinstance(item, dict) and isinstance(item.get("i"), int):
                    item["issues"] = [x for x in item.get("issues") or [] if x.get("category") in CATS]
                    out[item["i"]] = item
    cache.write_text(json.dumps(cached, ensure_ascii=False))
    return out
