"""The bounded result a worker must return.

A worker reports evidence, not transcript: a result that cannot be read at a
glance has moved the cost back into the session that delegated it.
"""

from __future__ import annotations

import json
from typing import Any

MAX_ITEMS = 5
# An item is one clause. Without a length cap a worker can honour the item
# count and still hand back a report, which is the cost we delegated to avoid.
MAX_ITEM_CHARS = 300
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
        elif any(len(item) > MAX_ITEM_CHARS for item in value):
            problems.append(f"{field} holds an item longer than {MAX_ITEM_CHARS} characters")

    decision = result["decision_needed"]
    if decision is not None and not isinstance(decision, str):
        problems.append("decision_needed must be a string or null")
    elif result["status"] == "decision_needed" and not decision:
        problems.append("status is decision_needed but decision_needed is empty")
    return problems


def from_text(text: str) -> dict[str, Any] | None:
    """The result object inside a worker's message, or None if there isn't one.

    Models wrap JSON in a code fence often enough that refusing a fenced answer
    would fail runs that did the work correctly, and one was seen introducing the
    fence with a sentence first. The fields are still checked either way; what is
    relaxed here is only how the object is found.
    """
    stripped = text.strip()
    for candidate in (stripped, _fenced(stripped)):
        if candidate is None:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _fenced(text: str) -> str | None:
    """Whatever sits inside the first code fence, wherever the fence starts."""
    opening = text.find("```")
    if opening == -1 or "\n" not in text[opening:]:
        return None
    body = text[opening:].split("\n", 1)[1]
    closing = body.rfind("```")
    return body[:closing] if closing != -1 else body


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
                    "items": {"type": "string", "maxLength": MAX_ITEM_CHARS},
                }
                for field in EVIDENCE_FIELDS
            },
            "decision_needed": {"type": ["string", "null"]},
        },
    }
