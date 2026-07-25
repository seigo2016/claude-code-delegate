"""The bounded result a worker must return.

A worker reports evidence, not transcript. The caps keep a result small enough to
read in the Claude Code session that asked for it, which is the whole point of
delegating the work in the first place.
"""

from __future__ import annotations

from typing import Any

MAX_ITEMS = 5
EVIDENCE_FIELDS = ("observed_facts", "verified_comparisons", "artifact_paths", "blockers")
FIELDS = ("status", *EVIDENCE_FIELDS, "decision_needed")
STATUSES = ("completed", "decision_needed")


def violations(result: Any) -> list[str]:
    """Every reason ``result`` is not a valid envelope, in reading order."""
    if not isinstance(result, dict):
        return ["result is not a JSON object"]

    problems: list[str] = []
    missing = [field for field in FIELDS if field not in result]
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    unexpected = [field for field in result if field not in FIELDS]
    if unexpected:
        problems.append(f"unexpected fields: {', '.join(sorted(unexpected))}")
    if missing or unexpected:
        return problems

    if result["status"] not in STATUSES:
        problems.append(f"status must be one of: {', '.join(STATUSES)}")
    for field in EVIDENCE_FIELDS:
        value = result[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f"{field} must be an array of strings")
        elif len(value) > MAX_ITEMS:
            problems.append(f"{field} holds more than {MAX_ITEMS} items")

    decision = result["decision_needed"]
    if decision is not None and not isinstance(decision, str):
        problems.append("decision_needed must be a string or null")
    elif result["status"] == "decision_needed" and not decision:
        problems.append("status is decision_needed but decision_needed is empty")
    return problems


def json_schema() -> dict[str, Any]:
    """The same contract, in the form a worker can be handed up front."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "delegate result",
        "type": "object",
        "additionalProperties": False,
        "required": list(FIELDS),
        "properties": {
            "status": {"type": "string", "enum": list(STATUSES)},
            **{
                field: {
                    "type": "array",
                    "maxItems": MAX_ITEMS,
                    "items": {"type": "string"},
                }
                for field in EVIDENCE_FIELDS
            },
            "decision_needed": {"type": ["string", "null"]},
        },
    }
