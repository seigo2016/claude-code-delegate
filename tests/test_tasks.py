from __future__ import annotations

from pathlib import Path

from delegate import config, envelope, tasks


def plan() -> config.Plan:
    role = config.Role(name="artifact-auditor", level="standard")
    return config.Plan(
        role=role, worker="claude", adapter="claude", model="sonnet", effort="high", timeout=60
    )


def test_the_prompt_states_the_result_shape_because_most_backends_cannot_be_handed_one(
    tmp_path: Path,
) -> None:
    prompt = tasks.compose_prompt(
        tmp_path,
        plan(),
        {
            "objective": "Check something.",
            "read": ["README.md"],
            "allowed_writes": [],
            "required_evidence": ["a count"],
            "host_only": False,
        },
    )

    for field in envelope.FIELDS:
        assert field in prompt
    assert "hard validity constraint" in prompt
    assert "within 240 characters" in prompt


def test_a_read_only_prompt_forbids_scratch_writes_as_well_as_repository_edits(
    tmp_path: Path,
) -> None:
    prompt = tasks.compose_prompt(
        tmp_path,
        plan(),
        {
            "objective": "Inspect something.",
            "read": ["README.md"],
            "allowed_writes": [],
            "required_evidence": ["a count"],
            "host_only": False,
        },
    )

    assert "This is a read-only task." in prompt
    assert "create files in .claude, /tmp, or elsewhere" in prompt


def test_a_writable_prompt_requires_exact_repository_relative_paths(tmp_path: Path) -> None:
    prompt = tasks.compose_prompt(
        tmp_path,
        plan(),
        {
            "objective": "Apply the approved change.",
            "read": ["README.md"],
            "allowed_writes": ["docs/decision.md"],
            "required_evidence": ["the edited path"],
            "host_only": False,
        },
    )

    assert "canonical repository-relative paths" in prompt
    assert "similarly named file elsewhere" in prompt
