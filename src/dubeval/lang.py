"""Language resolution: any user or container language label -> one record every layer uses.

Accepts BCP-47 tags (es-419, pt-BR, zh-Hant), ISO 639-1/2/3 codes (es, spa, chi, ger) and English
names ("Latin American Spanish", "hindi"). Nothing else in the pipeline hardcodes a language.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache

import langcodes

# Scripts written without spaces between words: ASR word tokens are joined with "" instead of " ".
NO_SPACE_SCRIPTS = frozenset({"Hani", "Hans", "Hant", "Jpan", "Thai", "Laoo", "Khmr", "Mymr", "Tibt"})
# Whisper uses a few legacy / macro-language codes.
_WHISPER_ALIAS = {"jv": "jw", "fil": "tl", "nb": "no"}
# Pairs Whisper language-ID cannot reliably tell apart; detection inside a group is not a mismatch.
_CONFUSABLE = (frozenset({"hi", "ur"}), frozenset({"sr", "hr", "bs"}), frozenset({"ms", "id"}),
               frozenset({"no", "nb", "nn", "da"}), frozenset({"zh", "yue"}))
_NOT_A_LANGUAGE = frozenset({"und", "mul", "zxx", "mis"})


@lru_cache(maxsize=1)
def _whisper_codes() -> frozenset[str]:
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    return frozenset(_LANGUAGE_CODES)


@dataclass(frozen=True)
class Lang:
    tag: str            # normalized BCP-47, e.g. "es-419"
    base: str           # language subtag, e.g. "es"
    name: str           # English display name, e.g. "Spanish (Latin America)"
    whisper: str | None  # Whisper language code, None = unsupported (ASR auto-detects)
    spaced: bool        # words separated by spaces

    @property
    def joiner(self) -> str:
        return " " if self.spaced else ""

    def to_dict(self) -> dict:
        return asdict(self)


def _language(x: langcodes.Language) -> langcodes.Language | None:
    lang = x.language
    if not lang or lang in _NOT_A_LANGUAGE or (len(lang) == 3 and "qaa" <= lang <= "qtz"):  # private use
        return None
    return x


def resolve(label: str) -> Lang:
    s = label.strip().replace("_", "-")
    try:
        x = _language(langcodes.get(s) if langcodes.tag_is_valid(s) else langcodes.find(s))
    except (LookupError, ValueError):
        x = None
    if x is None:
        raise ValueError(f"unknown language {label!r}: use a BCP-47 tag (es-419), ISO 639 code (spa) or English name")
    w = _WHISPER_ALIAS.get(x.language, x.language)
    script = x.script or x.maximize().script
    return Lang(tag=x.to_tag(), base=x.language, name=x.display_name(),
                whisper=w if w in _whisper_codes() else None, spaced=script not in NO_SPACE_SCRIPTS)


def base_of(tag: str | None) -> str | None:
    """Language subtag of a container/stream tag ('spa' -> 'es'); None for missing/'und'/garbage."""
    if not tag:
        return None
    s = tag.strip().replace("_", "-")
    if not langcodes.tag_is_valid(s):
        return None
    x = _language(langcodes.get(s))
    return x.language if x else None


def same_language(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = (base_of(v) or v for v in (a, b))
    return a == b or any(a in g and b in g for g in _CONFUSABLE)
