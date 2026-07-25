"""The result contract has exactly one definition.

Codex can be handed a JSON Schema so the worker self-validates, but OpenCode and
claude offer no equivalent, so our own check is the only guarantee that holds for
every backend. The schema shipped to workers is generated from that same check,
so the two cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from delegate import envelope

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "schemas" / "result.schema.json"


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


def test_the_shipped_schema_is_generated_from_the_same_definition() -> None:
    assert json.loads(SCHEMA_FILE.read_text(encoding="utf-8")) == envelope.json_schema()


def test_a_fenced_answer_is_still_an_answer() -> None:
    fenced = "```json\n" + json.dumps(valid()) + "\n```"

    assert envelope.from_text(fenced) == valid()
    assert envelope.from_text(json.dumps(valid())) == valid()


def test_prose_is_not_an_answer() -> None:
    assert envelope.from_text("I looked at the files and they seem fine.") is None
    assert envelope.from_text("[1, 2, 3]") is None


def test_a_single_overlong_item_cannot_smuggle_a_report_past_the_item_cap() -> None:
    packed = {**valid(), "observed_facts": ["x" * (envelope.MAX_ITEM_CHARS + 1)]}

    assert envelope.violations(packed) == [
        f"observed_facts holds an item longer than {envelope.MAX_ITEM_CHARS} characters"
    ]
