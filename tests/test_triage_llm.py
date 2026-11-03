from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from vreview import triage_llm
from vreview.schema import Issue
from vreview.storage.local import LocalStore
from vreview.triage_llm import write_comments

PREFIX = "reviews/run1/triage-llm"


@dataclass
class FakeThread:
    """Stands in for vreview.triage.Thread (same fields as the contract)."""
    id: str
    t0: float
    t1: float
    issue_ids: list[str]
    track: str | None = "hi->es"
    priority: str = "should-fix"
    areas: list[str] = field(default_factory=lambda: ["sync"])
    title: str = "rule title"
    comment: str = "rule comment"
    original_text: str | None = "नमस्ते"
    dubbed_text: str | None = "Hola"


def make_issue(n: int, **kw) -> Issue:
    base = dict(id=f"dubbing:hi->es:I{n:03d}", evaluator="dubbing", category="OVERRUN", severity="major",
                confidence=0.83, t0=float(n), t1=n + 1.5, track="hi->es",
                description=f"Dub keeps talking {n}.5s after the original line ended.",
                fix_hint=f"Shorten the translation by about {n}.5s.", details={"source": "timing"})
    return Issue(**{**base, **kw})


def make_review(n: int = 3, priorities: list[str] | None = None):
    issues = {i.id: i for i in (make_issue(k) for k in range(1, n + 1))}
    ids = list(issues)
    threads = [FakeThread(id=f"T{k + 1:03d}", t0=float(k + 1), t1=k + 2.5, issue_ids=[ids[k]],
                          priority=(priorities or [])[k] if priorities else "should-fix")
               for k in range(n)]
    return threads, issues


class FakeClient:
    """OpenAI-compatible stub. `handler(model, prompt)` returns the reply text or an Exception to raise."""

    def __init__(self, handler=None):
        self.handler = handler or ok_handler
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        out = self.handler(kw["model"], kw["messages"][0]["content"])
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=out))])

    @property
    def thread_calls(self) -> list[dict]:
        return [c for c in self.calls if not is_summary(c["messages"][0]["content"])]

    @property
    def summary_calls(self) -> list[dict]:
        return [c for c in self.calls if is_summary(c["messages"][0]["content"])]


def is_summary(prompt: str) -> bool:
    return '{"summary"' in prompt


def prompt_ids(prompt: str) -> list[str]:
    return re.findall(r'"id": "(T\d+)"', prompt)


def ok_handler(model: str, prompt: str) -> str:
    if is_summary(prompt):
        return json.dumps({"summary": "Two blockers drive **request_changes**."})
    return json.dumps({"threads": [{"id": i, "title": f"Title {i}", "comment": f"**{i}** is wrong; fix it."}
                                   for i in prompt_ids(prompt)]})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(triage_llm, "RETRY_DELAY_S", 0)


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "blobs")


def run(threads, issues, store, client, models=("m1",), **kw):
    return write_comments(threads, issues, src_lang="Hindi", tgt_lang="Spanish", models=list(models),
                          cache=store, cache_prefix=PREFIX, client=client, **kw)


def cache_files(store: LocalStore) -> list[Path]:
    return sorted((store.root / PREFIX).glob("*.json"))


def test_success_chunks_threads_and_writes_summary(store):
    threads, issues = make_review(12)
    client = FakeClient()

    res = run(threads, issues, store, client)

    assert res["warnings"] == []
    assert set(res["threads"]) == {t.id for t in threads}
    assert res["threads"]["T004"] == {"title": "Title T004", "comment": "**T004** is wrong; fix it.",
                                      "model": "m1"}
    assert res["summary_model"] == "m1"
    assert res["summary"] == "Two blockers drive **request_changes**."
    # 12 threads -> chunks of 10 + 2, plus one summary call
    assert sorted(len(prompt_ids(c["messages"][0]["content"])) for c in client.thread_calls) == [2, 10]
    assert len(client.summary_calls) == 1
    for c in client.calls:
        assert c["temperature"] == 0
        assert c["timeout"] == 60
        assert c["model"] == "m1"
    # one cache blob per call, keyed under the prefix
    assert len(cache_files(store)) == 3
    assert all(re.fullmatch(r"[0-9a-f]{64}\.json", p.name) for p in cache_files(store))


def test_prompt_is_grounded_in_issue_data(store):
    threads, issues = make_review(1)
    issue = next(iter(issues.values()))
    issue.details["visual"] = {"speaking_likely": True}
    client = FakeClient()

    run(threads, issues, store, client)

    prompt = client.thread_calls[0]["messages"][0]["content"]
    assert issue.description in prompt and issue.fix_hint in prompt
    assert "OVERRUN" in prompt and '"on_camera": true' in prompt
    assert "नमस्ते" in prompt and "Hola" in prompt
    assert "Hindi" in prompt and "Spanish" in prompt
    summary_prompt = client.summary_calls[0]["messages"][0]["content"]
    assert '"verdict": "approve"' in summary_prompt


def test_summary_prompt_carries_verdict_and_counts(store):
    threads, issues = make_review(3, priorities=["blocker", "nit", "needs-human"])
    client = FakeClient()

    run(threads, issues, store, client)

    data = json.loads(client.summary_calls[0]["messages"][0]["content"].split("Data:\n", 1)[1])
    assert data["verdict"] == "request_changes"
    assert data["thread_counts"] == {"blocker": 1, "nit": 1, "needs-human": 1}
    assert data["issue_count"] == 3
    assert data["key_threads"][0]["id"] == "T001"      # most serious first


def test_cache_hit_makes_no_call_and_needs_no_credentials(store, monkeypatch):
    threads, issues = make_review(12)
    first = run(threads, issues, store, FakeClient())

    def boom():
        raise AssertionError("client must not be built on a full cache hit")

    monkeypatch.setattr(triage_llm, "_client", boom)
    again = FakeClient(lambda m, p: AssertionError("no call expected"))
    second = run(threads, issues, store, again)

    assert again.calls == []
    assert second == first
    assert second["warnings"] == []


def test_cache_key_depends_on_models_and_inputs(store):
    threads, issues = make_review(2)
    run(threads, issues, store, FakeClient(), models=["m1"])
    n = len(cache_files(store))

    other_models = FakeClient()
    run(threads, issues, store, other_models, models=["m1", "m2"])
    assert len(other_models.calls) == 2                 # models changed -> asked again
    assert len(cache_files(store)) == 2 * n

    issues[threads[0].issue_ids[0]].description = "edited"
    changed = FakeClient()
    run(threads, issues, store, changed, models=["m1"])
    assert len(changed.thread_calls) == 1               # chunk input changed -> chunk asked again
    assert len(changed.summary_calls) == 0              # summary input (counts, titles) unchanged -> cached


def test_malformed_json_becomes_warning_and_keeps_summary(store):
    threads, issues = make_review(3)

    def handler(model, prompt):
        return "not json {" if not is_summary(prompt) else ok_handler(model, prompt)

    client = FakeClient(handler)
    res = run(threads, issues, store, client)

    assert res["threads"] == {}
    assert res["summary"] is not None
    assert len(res["warnings"]) == 1
    assert "T001-T003" in res["warnings"][0] and "m1" in res["warnings"][0]
    assert len(client.thread_calls) == 2                 # first try + one retry
    assert len(cache_files(store)) == 1                  # only the summary was cached


@pytest.mark.parametrize("reply", [
    '{"threads": "nope"}',
    '{"threads": [{"id": "T001", "title": "", "comment": "x"}]}',
    '{"threads": [{"id": "T001", "title": "t"}]}',
    '[]',
    '',
])
def test_invalid_shapes_are_rejected(store, reply):
    threads, issues = make_review(1)
    client = FakeClient(lambda m, p: ok_handler(m, p) if is_summary(p) else reply)

    res = run(threads, issues, store, client)

    assert res["threads"] == {}
    assert res["warnings"] and "T001" in res["warnings"][0]


def test_fenced_json_is_accepted(store):
    threads, issues = make_review(1)
    client = FakeClient(lambda m, p: "```json\n" + ok_handler(m, p) + "\n```")

    res = run(threads, issues, store, client)

    assert res["warnings"] == [] and res["threads"]["T001"]["title"] == "Title T001"
    assert res["summary"]


def test_model_fallback_after_retry(store):
    threads, issues = make_review(3)

    def handler(model, prompt):
        return TimeoutError("request timed out") if model == "m1" else ok_handler(model, prompt)

    client = FakeClient(handler)
    res = run(threads, issues, store, client, models=["m1", "m2"])

    assert res["warnings"] == []
    assert set(res["threads"]) == {"T001", "T002", "T003"}
    assert res["summary"]
    by_model = [c["model"] for c in client.calls]
    assert by_model.count("m1") == 4                     # 2 jobs x (try + retry)
    assert by_model.count("m2") == 2                     # one success per job


def test_retry_on_same_model_before_fallback(store):
    threads, issues = make_review(1)
    state = {"n": 0}

    def handler(model, prompt):
        if is_summary(prompt):
            return ok_handler(model, prompt)
        state["n"] += 1
        return RuntimeError("429") if state["n"] == 1 else ok_handler(model, prompt)

    client = FakeClient(handler)
    res = run(threads, issues, store, client, models=["m1", "m2"])

    assert res["warnings"] == []
    assert {c["model"] for c in client.calls} == {"m1"}  # the retry succeeded, m2 never used


def test_all_models_failing_never_raises(store):
    threads, issues = make_review(2)
    client = FakeClient(lambda m, p: ConnectionError("boom"))

    res = run(threads, issues, store, client, models=["m1", "m2"])

    assert res["threads"] == {} and res["summary"] is None
    assert len(res["warnings"]) == 2                     # chunk + summary
    assert all("m2: ConnectionError: boom" in w for w in res["warnings"])
    assert len(client.calls) == 8                        # 2 jobs x 2 models x 2 attempts
    assert cache_files(store) == []


def test_unknown_and_missing_ids(store):
    threads, issues = make_review(3)

    def handler(model, prompt):
        if is_summary(prompt):
            return ok_handler(model, prompt)
        return json.dumps({"threads": [
            {"id": "T001", "title": "ok one", "comment": "c1"},
            {"id": "T999", "title": "ghost", "comment": "invented"},
            {"id": "T001", "title": "dup", "comment": "second answer"},
            {"id": ["T002"], "title": "bad id type", "comment": "x"},
            "garbage",
            # T002 and T003 are missing
        ]})

    client = FakeClient(handler)
    res = run(threads, issues, store, client)

    assert res["threads"] == {"T001": {"title": "ok one", "comment": "c1", "model": "m1"}}
    assert "T999" not in json.dumps(res)
    assert len(res["warnings"]) == 1
    assert "T002, T003" in res["warnings"][0]
    assert len(client.thread_calls) == 1                 # partial answer is accepted, not retried
    assert len(cache_files(store)) == 1                  # incomplete chunk is not cached, summary is

    again = FakeClient()
    res2 = run(threads, issues, store, again)
    assert len(again.thread_calls) == 1 and len(again.summary_calls) == 0
    assert res2["warnings"] == [] and len(res2["threads"]) == 3


def test_ids_from_other_chunks_are_ignored(store):
    threads, issues = make_review(4)

    def handler(model, prompt):
        if is_summary(prompt):
            return ok_handler(model, prompt)
        mine = prompt_ids(prompt)
        wrong = "T004" if "T001" in mine else "T001"
        return json.dumps({"threads": [{"id": i, "title": i, "comment": i} for i in mine + [wrong]]})

    res = run(threads, issues, store, FakeClient(handler), chunk_size=2)

    assert set(res["threads"]) == {"T001", "T002", "T003", "T004"}
    assert res["warnings"] == []


def test_title_is_trimmed_to_80_chars(store):
    threads, issues = make_review(1)
    long_title = "word " * 40

    def handler(model, prompt):
        if is_summary(prompt):
            return ok_handler(model, prompt)
        return json.dumps({"threads": [{"id": "T001", "title": f"  {long_title}\n", "comment": "  body \n"}]})

    res = run(threads, issues, store, FakeClient(handler))

    assert len(res["threads"]["T001"]["title"]) <= 80
    assert res["threads"]["T001"]["comment"] == "body"


def test_threads_without_provided_issues_are_skipped(store):
    threads, issues = make_review(2)
    threads.append(FakeThread(id="T003", t0=9, t1=10, issue_ids=["dubbing:hi->es:I404"]))
    client = FakeClient()

    res = run(threads, issues, store, client)

    assert "T003" not in res["threads"] and set(res["threads"]) == {"T001", "T002"}
    assert any("T003" in w for w in res["warnings"])
    assert "T003" not in client.thread_calls[0]["messages"][0]["content"]


def test_no_models_empty_threads_and_missing_credentials(store, monkeypatch):
    threads, issues = make_review(2)
    client = FakeClient()

    res = run(threads, issues, store, client, models=[])
    assert res["threads"] == {} and res["summary"] is None and "no triage LLM models" in res["warnings"][0]

    assert run([], issues, store, client) == {"threads": {}, "summary": None, "warnings": [],
                                              "summary_model": None}
    assert client.calls == []

    def no_creds():
        raise KeyError("OPENAI_API_KEY")

    monkeypatch.setattr(triage_llm, "_client", no_creds)
    res = run(threads, issues, store, None)
    assert res["threads"] == {} and res["summary"] is None
    assert len(res["warnings"]) == 2 and "no LLM client" in res["warnings"][0]


def test_cache_write_failure_is_a_warning_not_an_error(store, monkeypatch):
    threads, issues = make_review(1)

    def broken_put(key, data, content_type=None):
        raise OSError("disk full")

    monkeypatch.setattr(store, "put_bytes", broken_put)
    res = run(threads, issues, store, FakeClient())

    assert res["threads"]["T001"]["title"] == "Title T001" and res["summary"]
    assert len(res["warnings"]) == 2 and all("could not write cache" in w for w in res["warnings"])


def test_corrupt_cache_entry_is_a_miss(store):
    threads, issues = make_review(1)
    run(threads, issues, store, FakeClient())
    for p in cache_files(store):
        p.write_text("{not json")
    client = FakeClient()

    res = run(threads, issues, store, client)

    assert len(client.calls) == 2 and res["warnings"] == []


def test_unexpected_error_is_contained(store):
    threads, issues = make_review(2)
    threads[1].areas = None  # type: ignore[assignment]  # malformed thread: skipped, and breaks the summary input
    client = FakeClient()

    res = run(threads, issues, store, client)

    assert res["threads"] == {} and res["summary"] is None
    assert any("T002" in w and "could not build LLM input" in w for w in res["warnings"])
    assert any("failed unexpectedly" in w for w in res["warnings"])
    assert all(isinstance(w, str) for w in res["warnings"])
    assert client.calls == []


# ------------------------------------------------------------------------------------------ verdict

def summary_data(client: FakeClient) -> dict:
    return json.loads(client.summary_calls[0]["messages"][0]["content"].split("Data:\n", 1)[1])


def test_given_verdict_overrides_recomputed_one(store):
    threads, issues = make_review(2, priorities=["blocker", "nit"])     # rule would say request_changes
    client = FakeClient()

    res = run(threads, issues, store, client, verdict="needs_human")

    assert summary_data(client)["verdict"] == "needs_human"
    assert res["summary"] and res["warnings"] == []


@pytest.mark.parametrize("verdict", [None, "", "   "])
def test_verdict_is_recomputed_when_missing_or_blank(store, verdict):
    threads, issues = make_review(2, priorities=["needs-human", "nit"])
    client = FakeClient()

    run(threads, issues, store, client, verdict=verdict)

    assert summary_data(client)["verdict"] == "needs_human"


def test_verdict_is_part_of_the_summary_cache_key(store):
    threads, issues = make_review(2, priorities=["blocker", "nit"])
    run(threads, issues, store, FakeClient(), verdict="request_changes")

    same = FakeClient(lambda m, p: AssertionError("no call expected"))
    run(threads, issues, store, same, verdict="request_changes")
    assert same.calls == []

    implicit = FakeClient(lambda m, p: AssertionError("no call expected"))
    run(threads, issues, store, implicit)             # same verdict as the recomputed one -> same key
    assert implicit.calls == []

    other = FakeClient()
    res = run(threads, issues, store, other, verdict="approve")
    assert len(other.summary_calls) == 1 and other.thread_calls == []   # only the summary is re-asked
    assert summary_data(other)["verdict"] == "approve"
    assert res["summary"] and len(res["threads"]) == 2


# ---------------------------------------------------------------------------------------- provenance

def test_model_that_answered_is_reported_per_chunk_and_for_summary(store):
    threads, issues = make_review(4)

    def handler(model, prompt):
        if is_summary(prompt):
            return ok_handler(model, prompt) if model == "m1" else TimeoutError("slow")
        ids = prompt_ids(prompt)
        if "T001" in ids and model == "m1":              # first chunk: m1 is down, m2 answers
            return TimeoutError("slow")
        return ok_handler(model, prompt)

    res = run(threads, issues, store, FakeClient(handler), models=["m1", "m2"], chunk_size=2)

    assert res["warnings"] == []
    assert {t: c["model"] for t, c in res["threads"].items()} == {"T001": "m2", "T002": "m2",
                                                                   "T003": "m1", "T004": "m1"}
    assert res["summary_model"] == "m1"


def test_summary_model_is_none_when_summary_fails(store):
    threads, issues = make_review(1)
    client = FakeClient(lambda m, p: ConnectionError("boom") if is_summary(p) else ok_handler(m, p))

    res = run(threads, issues, store, client)

    assert res["summary"] is None and res["summary_model"] is None
    assert res["threads"]["T001"]["model"] == "m1"


def test_provenance_survives_cache_hits(store):
    threads, issues = make_review(3)
    first = run(threads, issues, store, FakeClient(lambda m, p: TimeoutError("x") if m == "m1" else ok_handler(m, p)),
                models=["m1", "m2"])
    assert {c["model"] for c in first["threads"].values()} == {"m2"} and first["summary_model"] == "m2"

    again = FakeClient(lambda m, p: AssertionError("no call expected"))
    second = run(threads, issues, store, again, models=["m1", "m2"])

    assert again.calls == [] and second == first
    bodies = [json.loads(p.read_text()) for p in cache_files(store)]
    assert {b["model"] for b in bodies} == {"m2"}


def test_cache_entry_without_model_is_a_miss(store):
    threads, issues = make_review(1)
    run(threads, issues, store, FakeClient())
    for p in cache_files(store):
        body = json.loads(p.read_text())
        del body["model"]
        p.write_text(json.dumps(body))
    client = FakeClient()

    res = run(threads, issues, store, client)

    assert len(client.calls) == 2 and res["threads"]["T001"]["model"] == "m1"


# ----------------------------------------------------------------------------------------- grounding

def test_prompt_forbids_ungrounded_references_and_asks_one_sentence_per_issue(store):
    threads, issues = make_review(2)
    threads[0].issue_ids = list(issues)                  # T001 has both issues; T002 reuses none
    threads.pop()
    client = FakeClient()

    run(threads, issues, store, client)

    prompt = client.thread_calls[0]["messages"][0]["content"]
    assert "one sentence (or bullet) per entry in the thread's `issues`" in prompt
    assert "Never merge two issues into one sentence" in prompt
    assert "Refer only to things that appear in that thread's data" in prompt
    assert "as indicated in the script" in prompt        # quoted as the thing to avoid
    rows = json.loads(prompt.split("Threads:\n", 1)[1])
    assert len(rows[0]["issues"]) == 2                   # both member issues are in the input
    assert "do not refer to anything that is not in it" in client.summary_calls[0]["messages"][0]["content"]


def test_old_prompt_version_cache_entries_miss(store, monkeypatch):
    assert triage_llm.PROMPT_VERSION != "1"
    threads, issues = make_review(1)
    with monkeypatch.context() as m:
        m.setattr(triage_llm, "PROMPT_VERSION", "1")
        run(threads, issues, store, FakeClient())
    n_old = len(cache_files(store))

    client = FakeClient()
    run(threads, issues, store, client)

    assert len(client.calls) == 2                        # chunk and summary both asked again
    assert len(cache_files(store)) == 2 * n_old
