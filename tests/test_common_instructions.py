"""Instructions that apply to every role, written once."""

from __future__ import annotations

from pathlib import Path

import pytest

from delegate import config, tasks

BODY = b"""
[workers.codex]
adapter = "codex"
enabled = true
standard = { model = "terra", effort = "high" }

[roles.auditor]
level = "standard"
instructions = "ROLE-ONLY-TEXT"
"""
PACKET = {
    "objective": "Check something.",
    "read": ["README.md"],
    "allowed_writes": [],
    "required_evidence": ["a count"],
    "host_only": False,
}


def prompt_for(extra: bytes) -> str:
    settings = config.parse(extra + BODY, Path("delegate.toml"))
    return tasks.compose_prompt(Path("/repo"), settings.plan("auditor"), PACKET)


def test_common_instructions_precede_the_role_instructions_once() -> None:
    prompt = prompt_for(b'[common]\ninstructions = """COMMON-TEXT\nsecond line"""\n')

    assert prompt.count("COMMON-TEXT") == 1
    assert "second line" in prompt
    assert prompt.count("ROLE-ONLY-TEXT") == 1
    assert prompt.index("COMMON-TEXT") < prompt.index("Role instructions:")
    assert prompt.index("Role instructions:") < prompt.index("ROLE-ONLY-TEXT")


def test_without_common_instructions_the_prompt_has_no_such_section() -> None:
    prompt = prompt_for(b"")

    assert "common to all roles" not in prompt
    assert "ROLE-ONLY-TEXT" in prompt


def test_common_instructions_apply_to_a_role_with_none_of_its_own() -> None:
    settings = config.parse(
        b'[common]\ninstructions = "COMMON-TEXT"\n'
        + BODY.replace(b'instructions = "ROLE-ONLY-TEXT"', b""),
        Path("delegate.toml"),
    )
    prompt = tasks.compose_prompt(Path("/repo"), settings.plan("auditor"), PACKET)

    assert prompt.count("COMMON-TEXT") == 1
    assert "Role instructions:" not in prompt


@pytest.mark.parametrize(
    "bad", [b"common = 3\n", b"[common]\ninstructions = 3\n", b'[common]\ninstructions = " "\n']
)
def test_a_malformed_common_table_is_a_config_error(bad: bytes) -> None:
    with pytest.raises(config.ConfigError, match="common"):
        config.parse(bad + BODY, Path("delegate.toml"))
