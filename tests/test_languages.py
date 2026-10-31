"""Language resolution and dub-stream selection: the parts that must hold for any m x n language pair."""
import numpy as np
import pytest

from dubeval.lang import base_of, resolve, same_language
from dubeval.speech import text_in
from dubeval.tracks import choose_dubs, effective_lang, is_passthrough


@pytest.mark.parametrize("label,tag,whisper,spaced", [
    ("Latin American Spanish", "es-419", "es", True),
    ("es_419", "es-419", "es", True),
    ("spa", "es", "es", True),           # ISO 639-2 container tag
    ("ger", "de", "de", True),           # bibliographic 639-2/B
    ("pt-br", "pt-BR", "pt", True),
    ("zh-Hant", "zh-Hant", "zh", False),  # no-space script
    ("ja", "ja", "ja", False),
    ("th", "th", "th", False),
    ("jv", "jv", "jw", True),            # Whisper legacy code
    ("fil", "fil", "tl", True),
])
def test_resolve_maps_any_label_to_whisper_and_script(label, tag, whisper, spaced):
    lang = resolve(label)
    assert (lang.tag, lang.whisper, lang.spaced) == (tag, whisper, spaced)


def test_resolve_language_whisper_does_not_know_keeps_tag_and_disables_forced_asr():
    lang = resolve("tlh")
    assert lang.tag == "tlh" and lang.whisper is None


@pytest.mark.parametrize("bad", ["und", "xx-yy", "qaa"])
def test_resolve_rejects_non_languages(bad):
    with pytest.raises(ValueError):
        resolve(bad)


@pytest.mark.parametrize("tag", [None, "", "und", "qaa", "zxx", "not a tag"])
def test_unusable_container_tags_are_unknown(tag):
    assert base_of(tag) is None


def test_same_language_ignores_region_and_code_length_and_groups_confusables():
    assert same_language("spa", "es-419")
    assert same_language("hin", "ur")     # Whisper LID cannot separate spoken Hindi/Urdu
    assert not same_language("eng", "hi")
    assert not same_language(None, "hi")


def test_confident_detection_overrides_wrong_container_tag():
    # the 3 Idiots original is tagged 'eng' but speaks Hindi
    assert effective_lang("eng", {"lang": "hi", "prob": 0.97}) == "hi"
    assert effective_lang("spa", {"lang": "pt", "prob": 0.3}) == "es"
    assert effective_lang(None, {"lang": "pt", "prob": 0.3}) == "pt"


def _c(i, lang, passthrough=False):
    return {"index": i, "lang": lang, "passthrough": passthrough}


def test_choose_dubs_skips_source_copy_and_source_language_streams():
    picked, skipped = choose_dubs([_c(0, "es"), _c(1, "hi", passthrough=True)], "hi", [])
    assert picked == [(0, None)]
    assert skipped == [{"index": 1, "reason": "identical to original audio"}]


def test_choose_dubs_multi_track_filters_by_requested_languages():
    cands = [_c(0, "es"), _c(1, "pt"), _c(2, "fr"), _c(3, "hi")]
    picked, skipped = choose_dubs(cands, "hi", ["es", "pt"])
    assert picked == [(0, "es"), (1, "pt")]
    assert {s["index"] for s in skipped} == {2, 3}


def test_choose_dubs_lone_stream_in_source_language_is_still_evaluated():
    # a dub file whose only track is still Hindi is a defect to report, not a stream to skip
    picked, _ = choose_dubs([_c(0, "hi")], "hi", ["es"])
    assert picked == [(0, "es")]


def test_passthrough_detects_copied_audio_but_not_a_different_track():
    rng = np.random.default_rng(0)
    a = rng.standard_normal(16000 * 30).astype(np.float32)
    b = rng.standard_normal(16000 * 30).astype(np.float32)
    assert is_passthrough(a, np.roll(a, 160) * 0.5)  # 10 ms shifted, re-gained copy
    assert not is_passthrough(a, b)


def test_text_join_uses_no_spaces_for_cjk():
    words = [{"start": 0.0, "end": 0.2, "word": "こんにちは"}, {"start": 0.2, "end": 0.4, "word": "世界"}]
    assert text_in({"words": words, "joiner": ""}, 0, 1) == "こんにちは世界"
    assert text_in({"words": words, "joiner": " "}, 0, 1) == "こんにちは 世界"
