"""P4: collaboration plan contract (pure, offline)."""

import pytest

from agenthub.collab import MAX_PLAN_ITEMS, PlanItem, parse_plan_text, validate_items


def test_valid_items_are_numbered_and_dependencies_point_back():
    items = validate_items([{"agent_id": "codex", "goal": " Write tests "},
                            {"agent_id": "claude-code", "goal": "Review", "depends_on": [1, 1]}])
    assert items == [PlanItem(1, "codex", "Write tests", ()), PlanItem(2, "claude-code", "Review", (1,))]


@pytest.mark.parametrize("items", [
    [],
    [{"agent_id": "codex", "goal": f"g{i}"} for i in range(MAX_PLAN_ITEMS + 1)],
    [{"agent_id": "codex", "goal": "a", "depends_on": [1]}],          # itself
    [{"agent_id": "codex", "goal": "a", "depends_on": [2]}, {"agent_id": "codex", "goal": "b"}],  # later
    [{"agent_id": "codex", "goal": "  "}],
    [{"agent_id": "", "goal": "a"}],
])
def test_invalid_plans_are_refused(items):
    with pytest.raises(ValueError):
        validate_items(items)


def test_a_proposed_plan_is_read_leniently():
    reply = ("Here is my plan:\n"
             "1. codex: Write the parser tests\n"
             "- 2) [claude-code]: Review the tests (after 1)\n"
             "noise line\n"
             "3. gemini: Translate docs (after 1, 2, 7)\n")
    items = parse_plan_text(reply, agents=("codex", "claude-code"))
    assert [(i["agent_id"], i["goal"], i["depends_on"]) for i in items] == [
        ("codex", "Write the parser tests", []), ("claude-code", "Review the tests", [1]),
        ("gemini", "Translate docs", [1, 2])]
    assert items[2]["unknown_agent"] is True and "unknown_agent" not in items[0]
