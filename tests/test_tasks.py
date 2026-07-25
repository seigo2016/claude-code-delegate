from __future__ import annotations

from pathlib import Path

from delegate import config, envelope, tasks


def plan() -> config.Plan:
    role = config.Role(
        name="artifact-auditor", capability="standard", effort="high", task_class="review"
    )
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
    assert "```" in prompt or "code fence" in prompt
