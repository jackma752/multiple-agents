"""T-1d: the pre-send context preview contract (pure functions, offline)."""

import hashlib

import pytest

from agenthub.budget import estimate_visible_tokens
from agenthub.context import (
    FILE_SNIPPET, HANDOFF_CONTENT, HANDOFF_FRAME, SEPARATOR, USER_INPUT,
    check_preview, compose, part_texts, visible_estimate,
)
from agenthub.contracts import InvocationSnapshot
from agenthub_adapters.mock import MockAdapter
from agenthub_adapters.routing import RoutingAdapter
from agenthub_kernel.adapters import adapter_context_profile

PROFILE = {"adds_prompt_text": False,
           "observed": [{"evidence": "evidence/x", "binding": "v1", "input_total": 15183}]}
WHERE = {"message_id": 7, "agent_id": "codex"}
SEGMENTS = [(USER_INPUT, "Review 这段"), (SEPARATOR, "\n\n"),
            (HANDOFF_FRAME, "--- Handoff ---\n", WHERE), (HANDOFF_CONTENT, "OK", WHERE),
            (HANDOFF_FRAME, "\n--- End ---", WHERE)]


def preview_of(segments=SEGMENTS, **kw):
    return compose(segments, **kw)


def test_the_parts_are_an_exact_split_of_the_text_sent():
    text, p = preview_of(profile=PROFILE)
    assert text == "Review 这段\n\n--- Handoff ---\nOK\n--- End ---"
    assert "".join(part_texts(text, p)) == text
    assert [x["kind"] for x in p["parts"]] == [USER_INPUT, SEPARATOR, HANDOFF_FRAME, HANDOFF_CONTENT, HANDOFF_FRAME]
    assert p["chars"] == len(text) == sum(x["chars"] for x in p["parts"])
    assert [x["estimate"] for x in p["parts"]] == [estimate_visible_tokens(t) for t in part_texts(text, p)]
    assert p["estimate"] == sum(x["estimate"] for x in p["parts"])
    assert p["estimate"] >= estimate_visible_tokens(text)  # rounded up part by part
    assert p["parts"][3] == {"kind": HANDOFF_CONTENT, "chars": 2, "estimate": 1,
                             "sha256": hashlib.sha256(b"OK").hexdigest(), **WHERE}
    check_preview(text, p)


def test_empty_segments_are_dropped_and_unknown_kinds_refused():
    text, p = compose([(USER_INPUT, "x"), (SEPARATOR, "")])
    assert text == "x" and len(p["parts"]) == 1
    with pytest.raises(ValueError):
        compose([("summary", "x")])
    with pytest.raises(ValueError):
        compose([(HANDOFF_CONTENT, "x", {"path": "C:/secret"})])


def test_provider_side_context_is_unknown_and_never_estimated():
    _, independent = preview_of(profile=PROFILE)
    _, resumed = preview_of(profile=PROFILE, context_mode="native_resume")
    assert independent["provider_hidden"] == {"status": "unknown", "includes": ["instructions_and_tools"],
                                              "observed": PROFILE["observed"]}
    assert resumed["provider_hidden"]["includes"] == ["instructions_and_tools", "thread_history"]
    # The observation and the history are shown apart; the estimate is the parts' alone.
    assert resumed["estimate"] == independent["estimate"] == sum(x["estimate"] for x in resumed["parts"])


@pytest.mark.parametrize("profile, expected", [
    (PROFILE, {"chars": 0}),
    ({"adds_prompt_text": True}, {"status": "unknown"}),
    ({}, {"status": "unknown"}),
    (None, {"status": "unknown"}),
])
def test_adapter_text_is_zero_only_when_declared_verbatim(profile, expected):
    _, p = preview_of(profile=profile)
    assert p["adapter_text"] == expected
    if profile is None:
        assert "observed" not in p["provider_hidden"]


def _bump(p, i, key, by=1):
    parts = [dict(x) for x in p["parts"]]
    parts[i][key] += by
    return {**p, "parts": parts}


@pytest.mark.parametrize("tamper", [
    lambda p: {**p, "estimate": p["estimate"] + 1},
    lambda p: {**p, "chars": p["chars"] - 1},
    lambda p: _bump(p, 0, "estimate"),
    lambda p: _bump(p, 0, "chars"),
    lambda p: _bump(_bump(p, 0, "chars"), 1, "chars", -1),  # same total and estimates, wrong split
    lambda p: {**p, "parts": [{**p["parts"][0], "sha256": "0" * 64}, *p["parts"][1:]]},
    lambda p: {**p, "parts": p["parts"][:-1]},
    lambda p: {**p, "parts": [{**p["parts"][0], "kind": "summary"}, *p["parts"][1:]]},
    lambda p: {**p, "parts": [{**p["parts"][0], "path": "x"}, *p["parts"][1:]]},
    lambda p: {**p, "provider_hidden": {"status": "known", "tokens": 0}},
    lambda p: {**p, "version": 2},
    lambda p: {**p, "estimator": "tiktoken"},
])
def test_a_preview_that_does_not_describe_the_text_is_refused(tamper):
    text, p = preview_of(profile=PROFILE)
    with pytest.raises(ValueError):
        check_preview(text, tamper(p))
    with pytest.raises(ValueError):
        InvocationSnapshot(agent_id="codex", task_input=text, parent_task_id=None, depth=0,
                           context_preview=tamper(p))


def test_the_preview_is_bound_by_the_payload_hash():
    text, p = preview_of(profile=PROFILE)
    base = dict(agent_id="codex", task_input=text, parent_task_id=None, depth=0, policy_version=1)
    old = InvocationSnapshot(**base)
    assert "context_preview" not in old.to_dict()  # snapshots before T-1d keep their hash
    bound = InvocationSnapshot(**base, context_preview=p)
    assert bound.payload_hash() != old.payload_hash()
    _, other = preview_of(profile=None)  # same text, a different preview
    assert InvocationSnapshot(**base, context_preview=other).payload_hash() != bound.payload_hash()
    assert InvocationSnapshot(**bound.to_dict()).payload_hash() == bound.payload_hash()  # stored round trip


def test_the_visible_estimate_is_the_previews_or_the_whole_texts():
    text, p = preview_of(profile=PROFILE)
    assert visible_estimate({"task_input": text, "context_preview": p}) == p["estimate"]
    assert visible_estimate({"task_input": text}) == estimate_visible_tokens(text)
    assert visible_estimate({}) == 0


def test_file_snippets_are_a_reserved_part_kind():
    text, p = compose([(USER_INPUT, "see"), (SEPARATOR, "\n\n"), (FILE_SNIPPET, "def f(): pass")])
    check_preview(text, p)  # T-1f produces these; the contract already accepts them


class _Declares(MockAdapter):
    def context_profile(self):
        return dict(PROFILE)


class _Broken(MockAdapter):
    def context_profile(self):
        raise RuntimeError("boom")


def test_the_profile_comes_from_the_agents_own_adapter():
    assert adapter_context_profile(MockAdapter()) is None  # declares nothing: text unknown
    assert adapter_context_profile(_Broken()) is None
    router = RoutingAdapter({"codex": _Declares(), "mock": MockAdapter()})
    assert adapter_context_profile(router, "codex") == PROFILE
    assert adapter_context_profile(router, "mock") is None
    assert adapter_context_profile(router, "nobody") is None
