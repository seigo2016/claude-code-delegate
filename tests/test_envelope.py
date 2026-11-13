"""The result contract.

Only Codex can be handed a schema up front, so this check is the one guarantee
that holds for every backend.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

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


def test_an_unverified_result_is_accepted_as_distinct_from_completed() -> None:
    unverified = {
        **valid(),
        "status": "unverified",
        "blockers": [
            {
                "reason": "environment",
                "text": "pytest could not run because the dependency is unavailable",
            }
        ],
    }

    assert envelope.violations(unverified) == []
    assert "unverified" in envelope.STATUSES
    assert unverified["status"] != "completed"


def test_writable_contract_requires_a_boolean_work_complete() -> None:
    assert "work_complete" in envelope.json_schema(writes_allowed=True)["required"]
    assert "work_complete" not in envelope.json_schema()["required"]
    assert envelope.violations(valid(), writes_allowed=True) == []
    assert envelope.violations({**valid(), "work_complete": "yes"}, writes_allowed=True) == [
        "work_complete must be a boolean"
    ]
    assert (
        envelope.sanitize({**valid(), "work_complete": False}, writes_allowed=True)["work_complete"]
        is False
    )


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


def test_worker_schema_is_strict_at_every_object() -> None:
    def check(node: Any) -> None:
        if isinstance(node, dict):
            types = node.get("type")
            if types == "object" or isinstance(types, list) and "object" in types:
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    for writes_allowed in (False, True):
        schema = envelope.json_schema(writes_allowed=writes_allowed)
        check(schema)
        props = schema["properties"]
        assert "report_path" not in props
        assert props["report"]["type"] == ["string", "null"]
        assert props["counts"]["type"] == ["array", "null"]
        assert props["coverage"]["type"] == ["object", "null"]


def test_nullable_worker_fields_and_count_items_are_validated() -> None:
    result = {**valid(), "report": None, "counts": None, "coverage": None}
    assert envelope.violations(result) == []
    result["counts"] = [{"name": "passed", "count": 3}]
    assert envelope.violations(result) == []
    for bad in (
        {"passed": 3},
        [{"name": "passed", "count": True}],
        [{"name": "passed", "count": 3, "extra": 1}],
    ):
        assert envelope.violations({**result, "counts": bad})
    assert "report_path" not in envelope.sanitize({**result, "report_path": "forged"})


def test_legacy_write_result_has_unknown_completion_and_blocker() -> None:
    result = envelope.sanitize(valid(), writes_allowed=True)
    assert result["work_complete"] is None
    assert result["status"] == "unverified"
    assert "unknown" in result["blockers"][-1]["text"]
    assert envelope.violations(result, writes_allowed=True) == []


def test_unknown_completion_cannot_be_reported_completed() -> None:
    result = envelope.sanitize({**valid(), "work_complete": None}, writes_allowed=True)
    assert result["status"] == "unverified"
    assert envelope.sanitize(result, writes_allowed=True) == result


def blocker(reason: str, text: str = "x") -> dict[str, str]:
    return {"reason": reason, "text": text}


def test_legacy_string_blockers_become_not_done() -> None:
    cleaned = envelope.sanitize({**valid(), "blockers": ["could not run pytest"]})
    assert cleaned["blockers"] == [blocker("not_done", "could not run pytest")]
    assert envelope.violations(cleaned) == []


def test_an_unknown_or_missing_reason_is_not_done_never_more_lenient() -> None:
    cleaned = envelope.sanitize(
        {**valid(), "blockers": [{"reason": "whatever", "text": "a"}, {"text": "b"}]}
    )
    assert [b["reason"] for b in cleaned["blockers"]] == ["not_done", "not_done"]


def test_known_reasons_are_kept() -> None:
    blockers = [blocker(reason) for reason in envelope.BLOCKER_REASONS]
    blockers[0] = {**blockers[0], "forbidden_item": "editing config"}
    cleaned = envelope.sanitize({**valid(), "blockers": blockers}, forbidden=FORBIDDEN)
    assert sorted(cleaned["blockers"], key=str) == sorted(blockers, key=str)
    assert envelope.violations(cleaned) == []


@pytest.mark.parametrize(
    "bad",
    [
        "plain string",
        {"reason": "nope", "text": "x"},
        {"reason": "environment"},
        {"reason": "environment", "text": 3},
        {"reason": "environment", "text": "x", "extra": 1},
    ],
)
def test_malformed_blockers_are_violations(bad: object) -> None:
    assert envelope.violations({**valid(), "blockers": [bad]}) == [
        "blockers must be an array of {reason, text} objects"
    ]


def test_chair_display_truncates_blocker_text_and_keeps_the_reason() -> None:
    long = blocker("environment", "z" * 500)
    shown = envelope.chair_display({**valid(), "blockers": [long]})["blockers"][0]
    assert shown["reason"] == "environment"
    assert len(shown["text"]) == envelope.MAX_ITEM_CHARS and shown["text"].endswith("...")


def test_truncation_never_drops_a_not_done_blocker_behind_other_reasons() -> None:
    blockers = [blocker("environment", str(i)) for i in range(7)] + [blocker("not_done", "last")]
    cleaned = envelope.sanitize({**valid(), "blockers": blockers})
    assert len(cleaned["blockers"]) == envelope.MAX_ITEMS
    assert blocker("not_done", "last") in cleaned["blockers"]


def test_schema_describes_blockers_as_reasoned_objects() -> None:
    items = envelope.json_schema()["properties"]["blockers"]["items"]
    assert items["additionalProperties"] is False
    assert items["required"] == ["reason", "text", "forbidden_item"]
    assert items["properties"]["reason"]["enum"] == list(envelope.BLOCKER_REASONS)


@pytest.mark.parametrize(
    ("status", "work_complete", "reasons", "incomplete"),
    [
        ("completed", True, ["not_done"], True),
        ("completed", True, ["environment", "forbidden_by_packet"], False),
        ("completed", True, [], False),
        ("completed", False, [], True),
        ("unverified", True, ["not_done"], False),
        ("unverified", False, [], True),
    ],
)
def test_incomplete_write_work_reads_not_done_blockers(
    status: str, work_complete: bool, reasons: list[str], incomplete: bool
) -> None:
    result = {
        **valid(),
        "status": status,
        "work_complete": work_complete,
        "blockers": [blocker(reason) for reason in reasons],
    }
    assert envelope.incomplete_write_work(result, writes_allowed=True) is incomplete
    assert envelope.incomplete_write_work(result, writes_allowed=False) is False


@pytest.mark.parametrize("bad", [{"type": "x", "message": "y"}, {"text": 5}, 5, None, [1]])
def test_non_conforming_blockers_never_raise_and_are_violations(bad: object) -> None:
    cleaned = envelope.sanitize({**valid(), "blockers": [bad]}, writes_allowed=True)
    assert envelope.violations(cleaned, writes_allowed=True)
    assert envelope.chair_display(cleaned)


FORBIDDEN = ["running the network check", "editing config"]


def forbidden_blocker(item: object = "editing config") -> dict:
    return {"reason": "forbidden_by_packet", "text": "t", "forbidden_item": item}


def test_forbidden_by_packet_with_a_listed_item_is_kept() -> None:
    cleaned = envelope.sanitize({**valid(), "blockers": [forbidden_blocker()]}, forbidden=FORBIDDEN)
    assert cleaned["blockers"] == [forbidden_blocker()]
    assert envelope.violations(cleaned) == []


@pytest.mark.parametrize(
    "item", ["not listed", "Editing config", 5, None, ["editing config"], "editing config "]
)
def test_forbidden_by_packet_with_another_item_becomes_not_done(item: object) -> None:
    cleaned = envelope.sanitize(
        {**valid(), "blockers": [forbidden_blocker(item)]}, forbidden=FORBIDDEN
    )
    assert cleaned["blockers"] == [blocker("not_done", "t")]


def test_forbidden_by_packet_without_an_item_or_a_packet_list_becomes_not_done() -> None:
    bare = {"reason": "forbidden_by_packet", "text": "t"}
    assert envelope.sanitize({**valid(), "blockers": [bare]}, forbidden=FORBIDDEN)["blockers"] == [
        blocker("not_done", "t")
    ]
    assert envelope.sanitize({**valid(), "blockers": [forbidden_blocker()]})["blockers"] == [
        blocker("not_done", "t")
    ]


def test_chair_display_keeps_a_stored_forbidden_item() -> None:
    shown = envelope.chair_display({**valid(), "blockers": [forbidden_blocker()]})
    assert shown["blockers"] == [forbidden_blocker()]


def test_schema_allows_an_optional_forbidden_item() -> None:
    items = envelope.json_schema()["properties"]["blockers"]["items"]
    assert items["properties"]["forbidden_item"]["type"] == ["string", "null"]
    # A strict output schema lists every property as required; null stands for absent.
    assert "forbidden_item" in items["required"]


def test_a_null_forbidden_item_is_dropped() -> None:
    raw = {"reason": "environment", "text": "t", "forbidden_item": None}
    cleaned = envelope.sanitize({**valid(), "blockers": [raw]})
    assert cleaned["blockers"] == [blocker("environment", "t")]


@pytest.mark.parametrize("writes_allowed", [False, True])
def test_settle_makes_an_environment_blocker_on_completed_unverified(writes_allowed: bool) -> None:
    result = {**valid(), "status": "completed", "blockers": [blocker("environment")]}
    assert envelope.settle(result, writes_allowed=writes_allowed)["status"] == "unverified"
    assert envelope.has_environment_blocker(result)


def test_settle_leaves_a_valid_forbidden_by_packet_completed() -> None:
    result = {**valid(), "status": "completed", "blockers": [forbidden_blocker()]}
    assert envelope.settle(result, writes_allowed=False)["status"] == "completed"
    assert envelope.settle(result, writes_allowed=True)["status"] == "completed"


def test_settle_leaves_write_not_done_for_the_incomplete_work_path() -> None:
    result = {
        **valid(),
        "status": "completed",
        "work_complete": True,
        "blockers": [blocker("not_done"), blocker("environment")],
    }
    assert envelope.settle(result, writes_allowed=True)["status"] == "completed"


def test_chair_display_does_not_crash_on_malformed_counts_or_coverage() -> None:
    base = {
        "status": "completed",
        "observed_facts": [],
        "verified_comparisons": [],
        "artifact_paths": [],
        "blockers": [],
        "decision_needed": [],
    }
    for extra in (
        {"counts": [1, {"count": 2}, {"name": 3}]},
        {"coverage": {"done": 1, "total": 2, "remaining": [{"x": 1}, 5]}},
    ):
        shown = envelope.chair_display({**base, **extra})
        assert shown["status"] == "completed"
