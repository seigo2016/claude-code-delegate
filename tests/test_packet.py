from __future__ import annotations

from pathlib import Path
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


def test_an_unknown_field_is_refused_with_where_its_content_belongs() -> None:
    with pytest.raises(ValueError, match="unknown fields: requirements, context") as refused:
        packet.validate({**valid(), "requirements": ["replace line 3"], "context": "x"})
    assert "put instructions, constraints and deliverables in objective" in str(refused.value)


def test_every_known_optional_field_is_accepted() -> None:
    assert packet.validate(
        {
            **valid(),
            "read_optional": ["out.md"],
            "forbidden": ["push"],
            "escalation_reason": "x",
            "user_directed": True,
        }
    )


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


def test_read_optional_must_hold_strings() -> None:
    with pytest.raises(ValueError, match="read_optional must be an array of strings"):
        packet.validate({**valid(), "read_optional": [1]})
    assert packet.validate({**valid(), "read_optional": ["out/new.md"]})


def test_missing_reads_lists_concrete_paths_that_do_not_exist(tmp_path: Path) -> None:
    (tmp_path / "here.md").write_text("x")
    (tmp_path / "dir").mkdir()
    absolute_missing = str(tmp_path / "nope-abs")
    value = {
        **valid(),
        "read": ["here.md", "dir", "gone.md", absolute_missing, str(tmp_path / "here.md")],
    }

    assert packet.missing_reads(value, tmp_path) == ["gone.md", absolute_missing]


@pytest.mark.parametrize("entry", ["src/*.py", "docs/**/x.md", "a?.md", "[ab].md"])
def test_missing_reads_skips_globs(tmp_path: Path, entry: str) -> None:
    assert packet.missing_reads({**valid(), "read": [entry]}, tmp_path) == []


def test_missing_reads_ignores_urls_and_read_optional(tmp_path: Path) -> None:
    value = {**valid(), "read": ["https://example.test/x"], "read_optional": ["not/yet.md"]}
    assert packet.missing_reads(value, tmp_path) == []


def test_missing_reads_reports_an_entry_that_cannot_be_expanded(tmp_path: Path) -> None:
    value = {**valid(), "read": ["~nosuchuser-zz/x.md"]}
    assert packet.missing_reads(value, tmp_path) == ["~nosuchuser-zz/x.md"]
