"""Tests for vreview.triage: priority rule, areas, tags, thread clustering, verdict, rule text."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest

from dubeval.schema import Category
from vreview.schema import Brief, EvaluatorResult, Issue, Review, Triage
from vreview.triage import AREA_BY_CATEGORY, TriageConfig, issue_priority, triage

_seq = 0


def mk(
    category: str = "EARLY_CUTOFF",
    severity: str = "major",
    *,
    t0: float = 0.0,
    t1: float | None = None,
    confidence: float = 0.9,
    source: str | None = "timing",
    track: str | None = "hi->es",
    speaking: bool | None = None,
    id: str | None = None,
    original_text: str | None = None,
    dubbed_text: str | None = None,
    description: str | None = None,
    fix_hint: str | None = None,
) -> Issue:
    global _seq
    _seq += 1
    details: dict = {"source": source, "original_text": original_text, "dubbed_text": dubbed_text}
    details["visual"] = None if speaking is None else {"speaking_likely": speaking}
    return Issue(
        id=id or f"dubbing:{track}:I{_seq:03d}",
        evaluator="dubbing",
        category=category,
        severity=severity,
        confidence=confidence,
        t0=t0,
        t1=t0 + 1.0 if t1 is None else t1,
        track=track,
        description=description or f"{category} happened",
        fix_hint=fix_hint or f"fix {category}",
        details=details,
    )


def review_of(*issues: Issue, status: str = "ok", score: float | None = 50.0) -> Review:
    return Review(
        run_id="run_t",
        brief=Brief(kind="dub"),
        media=[],
        results=[EvaluatorResult(evaluator="dubbing", version="1", status=status, issues=list(issues), score=score)],
        score=score,
        created_at=datetime.now(timezone.utc),
    )


CFG = TriageConfig()


# --- priority -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs, expected",
    [
        # rule 1: language + critical is a blocker even at very low confidence
        (dict(category="WRONG_LANGUAGE", severity="critical", source="language", confidence=0.1), "blocker"),
        # rule 1 only covers critical; a major language issue falls through to the severity rule
        (dict(category="WRONG_LANGUAGE", severity="major", source="language", confidence=0.9), "should-fix"),
        (dict(category="WRONG_LANGUAGE", severity="major", source="language", confidence=0.3), "needs-human"),
        # rule 2: semantic below 0.6 -> needs-human, even though critical
        (dict(category="MEANING_MISMATCH", severity="critical", source="semantic", confidence=0.59), "needs-human"),
        (dict(category="MEANING_MISMATCH", severity="critical", source="semantic", confidence=0.6), "blocker"),
        (dict(category="MEANING_MISMATCH", severity="minor", source="semantic", confidence=0.5), "needs-human"),
        # the 0.6 threshold is for LLM findings only: a timing issue at 0.5 follows severity
        (dict(severity="critical", source="timing", confidence=0.5), "blocker"),
        # rule 3: any source below 0.4 -> needs-human; 0.4 itself is fine
        (dict(severity="major", source="timing", confidence=0.39), "needs-human"),
        (dict(severity="major", source="timing", confidence=0.4), "should-fix"),
        (dict(category="BACKGROUND_DROPOUT", severity="critical", source="audio", confidence=0.2), "needs-human"),
        # rule 4: severity
        (dict(severity="critical"), "blocker"),
        (dict(severity="major"), "should-fix"),
        (dict(severity="minor"), "nit"),
        # no details.source at all (issues from other evaluators / older data)
        (dict(severity="major", source=None), "should-fix"),
    ],
)
def test_issue_priority_rules(kwargs, expected):
    assert issue_priority(mk(**kwargs), CFG) == expected


def test_priority_thresholds_come_from_config():
    issue = mk("MEANING_MISMATCH", "major", source="semantic", confidence=0.7)
    assert issue_priority(issue, CFG) == "should-fix"
    assert issue_priority(issue, TriageConfig(semantic_min_confidence=0.8)) == "needs-human"
    issue = mk(severity="major", confidence=0.5)
    assert issue_priority(issue, TriageConfig(min_confidence=0.6)) == "needs-human"


def test_triage_does_not_touch_severity_times_or_score():
    issues = [mk("OVERRUN", "major", t0=3.0, t1=4.0, confidence=0.2), mk("LATE_START", "critical", t0=9.0, t1=9.5)]
    review = review_of(*issues, score=12.4)
    before = [(i.severity, i.t0, i.t1, i.confidence) for i in issues]
    triage(review)
    assert [(i.severity, i.t0, i.t1, i.confidence) for i in issues] == before
    assert review.score == 12.4


# --- areas and tags -----------------------------------------------------------------------------------------

CONTRACT_AREAS = {
    "accuracy": ["MEANING_MISMATCH", "MISSING_CONTENT", "ADDED_CONTENT", "NAME_OR_TERM_ERROR"],
    "sync": ["LATE_START", "EARLY_START", "EARLY_CUTOFF", "OVERRUN", "SYNC_DRIFT", "MID_LINE_GAP", "MISSING_DUB",
             "UNMATCHED_SPEECH", "DURATION_MISMATCH"],
    "audio-mix": ["BACKGROUND_DROPOUT", "ENERGY_MISMATCH"],
    "delivery": ["TONE_MISMATCH", "FAST_SPEECH"],
    "script": ["SCRIPT_DEVIATION", "SUBTITLE_AUDIO_DESYNC"],
    "language": ["WRONG_LANGUAGE", "UNTRANSLATED"],
    "video": ["VIDEO_MISMATCH"],
}


@pytest.mark.parametrize("area, categories", CONTRACT_AREAS.items())
def test_area_map_matches_contract(area, categories):
    for category in categories:
        review = review_of(mk(category))
        triage(review)
        assert review.results[0].issues[0].area == area, category


def test_every_dubeval_category_has_an_area_other_than_other():
    unmapped = [c for c in get_args(Category) if AREA_BY_CATEGORY.get(c, "other") == "other"]
    assert unmapped == []


def test_unknown_category_is_other():
    review = review_of(mk("SOMETHING_NEW"))
    tri = triage(review)
    assert review.results[0].issues[0].area == "other"
    assert tri.area_counts == {"other": 1}


def test_tags():
    plain = mk("OVERRUN", speaking=False)
    on_cam = mk("EARLY_CUTOFF", speaking=True, t0=100)
    unknown_face = mk("LATE_START", speaking=None, t0=200)
    llm = mk("MEANING_MISMATCH", source="semantic", t0=300)
    llm_on_cam = mk("MISSING_CONTENT", source="semantic", speaking=True, t0=400)
    triage(review_of(plain, on_cam, unknown_face, llm, llm_on_cam))
    assert plain.tags == ["area:sync"]
    assert on_cam.tags == ["area:sync", "on-camera"]
    assert unknown_face.tags == ["area:sync"]
    assert llm.tags == ["area:accuracy", "llm"]
    assert llm_on_cam.tags == ["area:accuracy", "on-camera", "llm"]


# --- threads ------------------------------------------------------------------------------------------------

def thread_sets(tri: Triage) -> list[list[str]]:
    return [t.issue_ids for t in tri.threads]


def test_overlapping_issues_share_a_thread_and_ids_follow_time():
    late = mk(t0=50, t1=51, id="late")
    a = mk(t0=10, t1=12, id="a")
    b = mk(t0=11, t1=13, id="b")  # overlaps a
    tri = triage(review_of(late, a, b))  # deliberately unsorted
    assert [t.id for t in tri.threads] == ["T001", "T002"]
    assert thread_sets(tri) == [["a", "b"], ["late"]]
    assert (tri.threads[0].t0, tri.threads[0].t1) == (10, 13)
    assert (a.thread_id, b.thread_id, late.thread_id) == ("T001", "T001", "T002")


def test_gap_of_exactly_0_3s_joins_and_more_splits():
    a = mk(t0=1.0, t1=2.0, id="a")
    joined = mk(t0=2.3, t1=3.0, id="joined")  # 0.3 s after a ends
    far = mk(t0=3.31, t1=4.0, id="far")  # 0.31 s after joined ends
    tri = triage(review_of(a, joined, far))
    assert thread_sets(tri) == [["a", "joined"], ["far"]]


def test_gap_is_measured_from_the_furthest_end_in_the_thread():
    long_issue = mk(t0=0, t1=10, id="long")
    inside = mk(t0=1, t1=2, id="inside")  # ends early, must not shrink the thread's end
    after = mk(t0=10.2, t1=11, id="after")
    tri = triage(review_of(long_issue, inside, after))
    assert thread_sets(tri) == [["long", "inside", "after"]]


def test_thread_span_is_capped_at_15s():
    # a chain of 2 s issues, each starting 1 s after the previous: contiguous, but 0 -> 21 s in total
    chain = [mk(t0=float(i), t1=i + 2.0, id=f"c{i:02d}") for i in range(0, 20, 1)]
    tri = triage(review_of(*chain))
    assert len(tri.threads) >= 2
    for thread in tri.threads:
        assert thread.t1 - thread.t0 <= 15.0 + 1e-9
    # nothing lost, nothing duplicated, still in time order
    assert [i for ids in thread_sets(tri) for i in ids] == [c.id for c in chain]
    assert tri.threads[0].t1 - tri.threads[0].t0 > 13  # the cap is hit, not a smaller accident


def test_exactly_15s_still_fits_and_just_over_splits():
    fits = triage(review_of(mk(t0=0, t1=8, id="a"), mk(t0=7, t1=15, id="b")))
    assert thread_sets(fits) == [["a", "b"]]
    over = triage(review_of(mk(t0=0, t1=8, id="a"), mk(t0=7, t1=15.01, id="b")))
    assert thread_sets(over) == [["a"], ["b"]]


def test_one_issue_longer_than_the_cap_gets_its_own_thread():
    huge = mk(t0=0, t1=38.65, id="huge")
    after = mk(t0=38.7, t1=39, id="after")
    tri = triage(review_of(huge, after))
    assert thread_sets(tri) == [["huge"], ["after"]]


def test_tracks_never_share_a_thread_and_ids_interleave_by_time():
    es = mk(t0=10, t1=12, track="hi->es", id="es1")
    fr = mk(t0=10, t1=12, track="hi->fr", id="fr1")
    es2 = mk(t0=30, t1=31, track="hi->es", id="es2")
    fr2 = mk(t0=20, t1=21, track="hi->fr", id="fr2")
    tri = triage(review_of(es, fr, es2, fr2))
    assert [(t.id, t.track, t.issue_ids) for t in tri.threads] == [
        ("T001", "hi->es", ["es1"]),
        ("T002", "hi->fr", ["fr1"]),
        ("T003", "hi->fr", ["fr2"]),
        ("T004", "hi->es", ["es2"]),
    ]


def test_thread_priority_is_the_highest_member_priority():
    # order of precedence: blocker > should-fix > needs-human > nit
    cases = [
        (["minor", "critical"], [0.9, 0.9], "blocker"),
        (["major", "critical"], [0.9, 0.9], "blocker"),
        (["major", "major"], [0.9, 0.2], "should-fix"),  # should-fix + needs-human -> should-fix
        (["minor", "major"], [0.9, 0.9], "should-fix"),
        (["minor", "minor"], [0.9, 0.2], "needs-human"),  # nit + needs-human -> needs-human
        (["minor", "minor"], [0.9, 0.9], "nit"),
    ]
    for severities, confidences, expected in cases:
        a = mk(severity=severities[0], confidence=confidences[0], t0=0, t1=1)
        b = mk(severity=severities[1], confidence=confidences[1], t0=0.5, t1=1.5)
        tri = triage(review_of(a, b))
        assert len(tri.threads) == 1
        assert tri.threads[0].priority == expected, (severities, confidences)


def test_thread_areas_list_the_primary_issue_first():
    nit_sync = mk("OVERRUN", "minor", t0=0, t1=1)
    blocker_accuracy = mk("MEANING_MISMATCH", "critical", source="semantic", t0=0.5, t1=1.5)
    tri = triage(review_of(nit_sync, blocker_accuracy))
    assert tri.threads[0].areas == ["accuracy", "sync"]


def test_primary_issue_among_equal_priorities_is_the_more_severe_then_more_confident():
    # both needs-human; the critical one names the thread even though the minor one is more confident
    minor = mk("OVERRUN", "minor", confidence=0.35, t0=0, t1=1)
    critical = mk("EARLY_CUTOFF", "critical", confidence=0.3, t0=0.5, t1=1.5)
    thread = triage(review_of(minor, critical)).threads[0]
    assert thread.priority == "needs-human"
    assert thread.title.startswith("Dub cuts off while the actor is still speaking")
    # same severity: the more confident one wins
    low = mk("OVERRUN", "major", confidence=0.7, t0=0, t1=1)
    high = mk("LATE_START", "major", confidence=0.95, t0=0.5, t1=1.5)
    assert triage(review_of(low, high)).threads[0].title.startswith("Dub starts late")


def test_thread_texts_are_joined_without_duplicates():
    a = mk(t0=0, t1=1, original_text="Hola", dubbed_text="Hello")
    b = mk(t0=0.5, t1=1.5, original_text="Hola", dubbed_text="Hello")  # same line again
    c = mk(t0=1.2, t1=2, original_text="Adios", dubbed_text=None)
    t = triage(review_of(a, b, c)).threads[0]
    assert t.original_text == "Hola Adios"
    assert t.dubbed_text == "Hello"
    nothing = triage(review_of(mk(t0=100))).threads[0]
    assert nothing.original_text is None and nothing.dubbed_text is None


# --- rule text ----------------------------------------------------------------------------------------------

def test_rule_text_is_built_from_the_member_issues():
    a = mk("EARLY_CUTOFF", "critical", t0=46.78, t1=48.35, speaking=True, description="Dub ends 1.57s early.",
           fix_hint="Extend the translation.", original_text="बलादकर", dubbed_text="violación")
    b = mk("LATE_START", "major", t0=47.0, t1=48.0, description="Dub starts 0.4s late.", fix_hint="Shift it earlier.")
    t = triage(review_of(a, b)).threads[0]
    assert t.title == "Dub cuts off while the actor is still speaking (+1 related)"
    assert t.comment_source == "rule"
    for expected in ("Dub ends 1.57s early.", "Dub starts 0.4s late.", "Extend the translation.",
                     "Shift it earlier.", "**Why it matters:**", "speaking face is visible", "बलादकर", "violación"):
        assert expected in t.comment, expected


def test_single_issue_title_has_no_related_suffix_and_every_category_has_a_title():
    t = triage(review_of(mk("EARLY_CUTOFF"))).threads[0]
    assert t.title == "Dub cuts off while the actor is still speaking"
    for category in get_args(Category):
        title = triage(review_of(mk(category))).threads[0].title
        assert title and "_" not in title, category


def test_needs_human_and_llm_notes():
    unsure = triage(review_of(mk("MEANING_MISMATCH", "major", source="semantic", confidence=0.5))).threads[0]
    assert unsure.priority == "needs-human"
    assert "Needs a human" in unsure.comment and "0.50" in unsure.comment
    confident_llm = triage(review_of(mk("MEANING_MISMATCH", "major", source="semantic", confidence=0.9))).threads[0]
    assert "LLM" in confident_llm.comment and "Needs a human" not in confident_llm.comment


def test_long_quotes_are_clipped():
    t = triage(review_of(mk(original_text="x" * 5000))).threads[0]
    assert len(t.comment) < 1500 and "…" in t.comment


# --- verdict, counts, summary -------------------------------------------------------------------------------

def test_verdict_request_changes_when_any_blocker_thread():
    tri = triage(review_of(mk(severity="critical", t0=0), mk(severity="minor", t0=50), mk(confidence=0.1, t0=90)))
    assert tri.verdict == "request_changes"
    assert tri.counts == {"blocker": 1, "should-fix": 0, "needs-human": 1, "nit": 1}


def test_verdict_needs_human_without_blockers():
    tri = triage(review_of(mk(severity="major", t0=0), mk(confidence=0.1, t0=50)))
    assert tri.verdict == "needs_human"
    assert tri.counts == {"blocker": 0, "should-fix": 1, "needs-human": 1, "nit": 0}


def test_verdict_approve_with_only_should_fix_and_nits():
    tri = triage(review_of(mk(severity="major", t0=0), mk(severity="minor", t0=50)))
    assert tri.verdict == "approve"
    assert tri.counts == {"blocker": 0, "should-fix": 1, "needs-human": 0, "nit": 1}


def test_verdict_approve_without_issues():
    tri = triage(review_of())
    assert tri.verdict == "approve"
    assert tri.threads == []
    assert tri.counts == {"blocker": 0, "should-fix": 0, "needs-human": 0, "nit": 0}
    assert tri.area_counts == {}
    assert "No issues" in tri.summary


def test_a_needs_human_issue_inside_a_should_fix_thread_does_not_raise_the_verdict():
    # thread priority is the highest member priority, so this thread is should-fix
    tri = triage(review_of(mk(severity="major", t0=0, t1=1), mk(severity="minor", confidence=0.1, t0=0.5, t1=1.5)))
    assert tri.threads[0].priority == "should-fix"
    assert tri.verdict == "approve"


def test_counts_are_thread_counts_and_area_counts_list_largest_first():
    issues = [
        mk("OVERRUN", t0=0, t1=1), mk("LATE_START", t0=0.5, t1=1.5),  # one sync thread (2 issues)
        mk("EARLY_START", t0=20), mk("SYNC_DRIFT", t0=40),  # two more sync threads
        mk("MEANING_MISMATCH", source="semantic", t0=60),  # one accuracy thread
    ]
    tri = triage(review_of(*issues))
    assert sum(tri.counts.values()) == len(tri.threads) == 4
    assert list(tri.area_counts.items()) == [("sync", 3), ("accuracy", 1)]


def test_summary_is_short_markdown_with_verdict_and_counts():
    issues = [mk("EARLY_CUTOFF", "critical", t0=27.71, t1=29.31)] + [mk("OVERRUN", t0=60 + 10 * i) for i in range(4)]
    tri = triage(review_of(*issues, score=12.4))
    assert tri.summary_source == "rule" and tri.llm_warnings == []
    assert tri.summary.startswith("**Changes requested.**")
    assert "5 threads" in tri.summary and "1 blocker" in tri.summary and "4 should-fix" in tri.summary
    assert "0:27.7" in tri.summary and "12.4/100" in tri.summary
    sentences = [s for s in tri.summary.replace("/100.", "/100").split(". ") if s]
    assert 2 <= len(sentences) <= 5


@pytest.mark.parametrize("status", ["skipped", "failed"])
def test_verdict_is_needs_human_when_no_evaluator_completed(status):
    review = review_of(status=status, score=None)
    review.results[0].reason = "not a dub"
    tri = triage(review)
    assert tri.verdict == "needs_human"
    assert tri.threads == []
    assert tri.counts == {"blocker": 0, "should-fix": 0, "needs-human": 0, "nit": 0}
    assert tri.summary.startswith("**Needs a human look.** No evaluator completed")
    assert f"dubbing ({status})" in tri.summary


def test_verdict_is_needs_human_when_there_are_no_evaluator_results_at_all():
    review = review_of()
    review.results = []
    tri = triage(review)
    assert tri.verdict == "needs_human"
    assert "No evaluator completed" in tri.summary


def test_one_completed_evaluator_is_enough_to_approve():
    review = review_of()  # dubbing ran and found nothing
    review.results.append(EvaluatorResult(evaluator="identity", version="1", status="skipped", reason="n/a"))
    tri = triage(review)
    assert tri.verdict == "approve"
    assert "No evaluator completed" not in tri.summary
    assert "Did not complete: identity (skipped)" in tri.summary


def test_issues_still_decide_the_verdict_when_an_evaluator_completed():
    review = review_of(mk(severity="critical"))
    review.results.append(EvaluatorResult(evaluator="identity", version="1", status="failed", reason="boom"))
    assert triage(review).verdict == "request_changes"


# --- mutation, idempotence, serialisation -------------------------------------------------------------------

def test_triage_sets_issue_fields_and_leaves_review_triage_to_the_caller():
    issue = mk("OVERRUN", "major", speaking=True)
    review = review_of(issue)
    tri = triage(review)
    assert (issue.priority, issue.area, issue.thread_id) == ("should-fix", "sync", "T001")
    assert issue.tags == ["area:sync", "on-camera"]
    assert review.triage is None
    assert tri.threads[0].issue_ids == [issue.id]


def test_triage_is_idempotent():
    review = review_of(mk("OVERRUN", t0=0), mk("MEANING_MISMATCH", source="semantic", t0=0.5), mk(t0=40))
    first = triage(review).model_dump()
    tags_after_first = [list(i.tags) for i in review.results[0].issues]
    second = triage(review).model_dump()
    assert first == second
    assert [list(i.tags) for i in review.results[0].issues] == tags_after_first


def test_old_reviews_without_triage_fields_still_parse():
    old = {
        "schema_version": "vreview.v1",
        "run_id": "run_old",
        "brief": {"kind": "dub"},
        "media": [],
        "results": [{
            "evaluator": "dubbing", "version": "1", "status": "ok", "score": 80.0,
            "issues": [{
                "id": "dubbing:hi->es:I001", "evaluator": "dubbing", "category": "OVERRUN", "severity": "major",
                "confidence": 0.8, "t0": 1.0, "t1": 2.0, "track": "hi->es", "description": "d", "fix_hint": "f",
            }],
        }],
        "score": 80.0,
        "created_at": "2026-10-31T22:00:00Z",
    }
    review = Review.model_validate(old)
    issue = review.results[0].issues[0]
    assert review.triage is None
    assert (issue.priority, issue.area, issue.thread_id, issue.tags) == (None, None, None, [])
    review.triage = triage(review)
    assert review.triage.verdict == "approve"
    assert issue.priority == "should-fix"


def test_review_with_triage_round_trips_through_json():
    review = review_of(mk("EARLY_CUTOFF", "critical", speaking=True), mk("MEANING_MISMATCH", source="semantic", t0=30))
    review.triage = triage(review)
    again = Review.model_validate_json(review.model_dump_json())
    assert again == review
    assert again.triage.threads[0].comment_model is None and again.triage.summary_model is None


# --- the real report (kept out of git with out/, so only runs where it exists) ------------------------------

REAL_REPORT = Path(__file__).resolve().parents[1] / "out/rerun-20261102/es-v1/result.json"


@pytest.mark.skipif(not REAL_REPORT.exists(), reason="real report not present")
def test_real_report_triage_invariants():
    result = EvaluatorResult.model_validate(json.loads(REAL_REPORT.read_text()))
    review = Review(run_id="real", brief=Brief(kind="dub", source_language="hi", target_languages=["es"]), media=[],
                    results=[result], score=result.score, created_at=datetime.now(timezone.utc))
    tri = triage(review)
    issues = result.issues
    assert issues and tri.threads
    # every issue is in exactly one thread, every thread id resolves
    members = [i for t in tri.threads for i in t.issue_ids]
    assert sorted(members) == sorted(i.id for i in issues)
    assert {i.thread_id for i in issues} == {t.id for t in tri.threads}
    assert all(i.priority and i.area for i in issues)
    for t in tri.threads:
        assert t.title and t.comment
        by_id = {i.id: i for i in issues}
        assert t.t0 == min(by_id[i].t0 for i in t.issue_ids)
    # thread span is only above the cap when one issue alone is that long
    for t in tri.threads:
        if t.t1 - t.t0 > 15 + 1e-9:
            assert len(t.issue_ids) == 1
    assert sum(tri.counts.values()) == len(tri.threads)
    assert (tri.verdict == "request_changes") == (tri.counts["blocker"] > 0)
