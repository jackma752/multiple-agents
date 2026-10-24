"""T-2: the stage summary contract (pure, offline)."""

import pytest

from agenthub.summary import MAX_FIELD_CHARS, StageSummary, parse_summary_text

FULL = StageSummary(goal="Ship file references", constraints="No real calls without approval",
                    done="Preview and budget", open_issues="@diff not done",
                    next_steps="Ask for T-1h approval", file_refs=("src/app.py:L1-20", '"docs/a b.md"'))


def test_the_body_has_fixed_headings_and_skips_empty_sections():
    body = StageSummary(goal="g", next_steps="n").body()
    assert body == "Goal:\ng\n\nNext steps:\nn"
    full = FULL.body()
    assert full.index("Goal:") < full.index("Constraints:") < full.index("Done:") \
        < full.index("Open issues:") < full.index("Next steps:") < full.index("Files:")
    assert "- src/app.py:L1-20" in full


def test_a_body_parses_back_into_the_same_fields():
    assert parse_summary_text(FULL.body()) == FULL.to_dict()


def test_a_drafted_reply_with_markdown_headings_and_junk_is_read_leniently():
    reply = ("Here is the summary.\n\n## Goal:\nFinish T-2\n\n**Constraints:**\n- keep approvals\n\n"
             "Next steps: write tests\n\nFiles:\n- src/app.py:L3-9\n- not a ref \"x\n- `docs/x.md`\n")
    draft = parse_summary_text(reply)
    assert draft["goal"] == "Finish T-2" and draft["constraints"] == "- keep approvals"
    assert draft["next_steps"] == "write tests" and draft["done"] == ""
    assert draft["file_refs"] == ["src/app.py:L3-9", "docs/x.md"]


@pytest.mark.parametrize("kwargs", [
    {"goal": "  "},
    {"goal": "g", "done": "x" * (MAX_FIELD_CHARS + 1)},
    {"goal": "g", "file_refs": ("a.py:L9-3",)},
    {"goal": "g", "file_refs": tuple(f"f{i}.py" for i in range(9))},
    {"goal": 5},
])
def test_invalid_summaries_are_refused(kwargs):
    with pytest.raises(ValueError):
        StageSummary(**kwargs)
