"""dubeval.judge: fallback vs ensemble, provenance, failures surface as warnings."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dubeval import judge as J


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(J.time, "sleep", lambda s: None)


def client(answers):
    def create(model, messages, **kw):
        a = answers[model]
        if isinstance(a, Exception):
            raise a
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"items": a})))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


ROWS = [{"i": 0, "orig": "a", "dub": "b", "script": None}, {"i": 1, "orig": "c", "dub": "d", "script": None}]


def item(i, *cats, sev="minor"):
    return {"i": i, "orig_en": "x", "dub_en": "y",
            "issues": [{"category": c, "severity": sev, "confidence": 0.7, "explanation": c} for c in cats]}


def test_fallback_keeps_first_answer_and_records_model(monkeypatch, tmp_path):
    monkeypatch.setattr(J, "_client", lambda: client({"m1": RuntimeError("429"), "m2": [item(0, "MISSING_CONTENT")]}))
    w = []
    out = J.judge(ROWS, ["m1", "m2"], "hi", "es", tmp_path / "c.json", w)
    assert out[0]["model"] == "m2" and w == []


def test_ensemble_merges_and_boosts_agreement(monkeypatch, tmp_path):
    monkeypatch.setattr(J, "_client", lambda: client({
        "m1": [item(0, "MISSING_CONTENT"), item(1)],
        "m2": [item(0, "MISSING_CONTENT", "TONE_MISMATCH", sev="major"), item(1, "NOT_A_CATEGORY")]}))
    out = J.judge(ROWS, ["m1", "m2"], "hi", "es", tmp_path / "c.json", [], mode="ensemble")
    by_cat = {x["category"]: x for x in out[0]["issues"]}
    assert set(by_cat) == {"MISSING_CONTENT", "TONE_MISMATCH"}
    assert by_cat["MISSING_CONTENT"]["models"] == ["m1", "m2"] and by_cat["MISSING_CONTENT"]["severity"] == "major"
    assert by_cat["MISSING_CONTENT"]["confidence"] == pytest.approx(0.8)
    assert by_cat["TONE_MISMATCH"]["models"] == ["m2"] and out[1]["issues"] == []


def test_ensemble_partial_and_total_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(J, "_client", lambda: client({"m1": RuntimeError("down"), "m2": [item(0, "MISSING_CONTENT")]}))
    w = []
    out = J.judge(ROWS, ["m1", "m2"], "hi", "es", tmp_path / "a.json", w, mode="ensemble")
    assert out[0]["issues"] and any("model skipped" in x for x in w) and not any("judge failed" in x for x in w)

    monkeypatch.setattr(J, "_client", lambda: client({"m1": RuntimeError("down"), "m2": RuntimeError("down")}))
    w = []
    assert J.judge(ROWS, ["m1", "m2"], "hi", "es", tmp_path / "b.json", w, mode="ensemble") == {}
    assert any(x.startswith("LLM judge failed") for x in w)
