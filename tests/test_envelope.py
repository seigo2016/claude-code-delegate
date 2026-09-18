"""The result contract.

Only Codex can be handed a schema up front, so this check is the one guarantee
that holds for every backend.
"""

from __future__ import annotations

import json
from typing import Any

from delegate import envelope


def valid() -> dict[str, Any]:
    return {
        "status": "completed",
        "observed_facts": ["tests/test_store.py has 2 tests"],
        "verified_comparisons": ["counted 2 test functions, matches the report"],
        "artifact_paths": ["tests/test_store.py"],
        "blockers": [],
        "decision_needed": None,
    }


def test_a_well_formed_result_is_accepted() -> None:
    assert envelope.violations(valid()) == []


def test_unknown_and_missing_keys_are_named_in_the_violation() -> None:
    extra = {**valid(), "raw_log": "..."}
    assert envelope.violations(extra) == ["unexpected fields: raw_log"]

    missing = valid()
    del missing["blockers"]
    assert envelope.violations(missing) == ["missing fields: blockers"]


def test_an_evidence_list_longer_than_the_cap_is_rejected() -> None:
    flooded = {**valid(), "observed_facts": [f"fact {i}" for i in range(envelope.MAX_ITEMS + 1)]}

    assert envelope.violations(flooded) == [
        f"observed_facts holds more than {envelope.MAX_ITEMS} items"
    ]


def test_a_result_that_asks_for_a_decision_must_say_what_the_decision_is() -> None:
    undecided = {**valid(), "status": "decision_needed", "decision_needed": None}

    assert envelope.violations(undecided) == [
        "status is decision_needed but decision_needed is empty"
    ]


def test_a_fenced_answer_is_still_an_answer() -> None:
    fenced = "```json\n" + json.dumps(valid()) + "\n```"

    assert envelope.from_text(fenced) == valid()
    assert envelope.from_text(json.dumps(valid())) == valid()


def test_an_answer_introduced_by_a_sentence_is_still_an_answer() -> None:
    announced = (
        "Based on my analysis, here is the result:\n\n```json\n" + json.dumps(valid()) + "\n```"
    )

    assert envelope.from_text(announced) == valid()


def test_prose_is_not_an_answer() -> None:
    assert envelope.from_text("I looked at the files and they seem fine.") is None
    assert envelope.from_text("[1, 2, 3]") is None


def test_a_single_overlong_item_cannot_smuggle_a_report_past_the_item_cap() -> None:
    packed = {**valid(), "observed_facts": ["x" * (envelope.MAX_ITEM_CHARS + 1)]}

    assert envelope.violations(packed) == [
        f"observed_facts holds an item longer than {envelope.MAX_ITEM_CHARS} characters"
    ]


def test_sanitize_truncates_overlong_items_and_caps_item_count() -> None:
    overlong = "a" * (envelope.MAX_ITEM_CHARS + 50)
    flooded = [f"item {i}: {overlong}" for i in range(envelope.MAX_ITEMS + 3)]
    raw = {**valid(), "observed_facts": flooded}

    cleaned = envelope.sanitize(raw)
    assert len(cleaned["observed_facts"]) == envelope.MAX_ITEMS
    for item in cleaned["observed_facts"]:
        assert len(item) == envelope.MAX_ITEM_CHARS
        assert item.endswith("...")
    assert envelope.violations(cleaned) == []


def test_sanitize_strips_unexpected_fields() -> None:
    extra = {**valid(), "toolAction": "viewing", "toolSummary": "view", "reasoning": "thought"}
    cleaned = envelope.sanitize(extra)
    for unexpected in ("toolAction", "toolSummary", "reasoning"):
        assert unexpected not in cleaned
    assert envelope.violations(cleaned) == []


def test_sanitize_defaults_blockers_and_decision_needed_on_completed() -> None:
    partial = {
        "status": "completed",
        "observed_facts": ["fact 1"],
        "verified_comparisons": ["comp 1"],
        "artifact_paths": ["a.txt"],
    }
    cleaned = envelope.sanitize(partial)
    assert cleaned["blockers"] == []
    assert cleaned["decision_needed"] is None
    assert envelope.violations(cleaned) == []


def test_from_text_extracts_json_without_code_fence() -> None:
    message = "I have launched the run and completed the task.\n" + json.dumps(valid())
    assert envelope.from_text(message) == valid()

