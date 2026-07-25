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


def test_a_well_formed_packet_is_accepted() -> None:
    assert packet.validate(valid()) == valid()


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


def test_a_host_only_packet_is_never_handed_to_an_external_worker() -> None:
    refusal = packet.refusal(valid())
    assert refusal is None

    refusal = packet.refusal({**valid(), "host_only": True})

    assert refusal is not None
    message, detail = refusal
    assert "host_only" in message
    assert detail == {"run_in": "claude-code"}
