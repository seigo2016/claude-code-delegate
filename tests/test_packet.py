from __future__ import annotations

from typing import Any

import pytest

from delegate import packet


def valid() -> dict[str, Any]:
    return {
        "objective": "Check that the release notes match the tags.",
        "read": ["CHANGELOG.md"],
        "allowed_writes": [],
        "required_evidence": ["tag list", "verdict"],
        "host_only": False,
    }


def test_every_required_field_must_be_present() -> None:
    incomplete = valid()
    del incomplete["host_only"]

    with pytest.raises(ValueError, match="missing fields: host_only"):
        packet.validate(incomplete)


def test_an_empty_objective_is_rejected() -> None:
    with pytest.raises(ValueError, match="objective"):
        packet.validate({**valid(), "objective": "   "})


def test_path_lists_must_hold_strings() -> None:
    with pytest.raises(ValueError, match="read must be an array of strings"):
        packet.validate({**valid(), "read": ["ok", 3]})


def test_escalation_reason_must_be_a_nonempty_string_when_present() -> None:
    with pytest.raises(ValueError, match="escalation_reason"):
        packet.validate({**valid(), "escalation_reason": ""})


def test_user_directed_must_be_a_boolean_when_present() -> None:
    with pytest.raises(ValueError, match="user_directed"):
        packet.validate({**valid(), "user_directed": "true"})
