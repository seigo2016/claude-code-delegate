from __future__ import annotations

from pathlib import Path

import pytest

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
    assert "exactly these six keys" in prompt


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
    assert "five highest-priority findings" in prompt
    assert "write it to a file" not in prompt


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
    assert "write it to a file you were allowed to write" in prompt


def packet(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "objective": "Inspect something.",
        "read": ["README.md"],
        "allowed_writes": [],
        "required_evidence": ["a count"],
        "host_only": False,
    }
    value.update(updates)
    return value


def settings(role: config.Role) -> config.Settings:
    return config.Settings(
        roles={role.name: role},
        workers={
            "claude": config.Worker(
                name="claude",
                adapter="claude",
                enabled=True,
                levels={"standard": config.Level(model="sonnet", effort="high")},
            )
        },
        default_worker="claude",
    )


def test_read_only_role_refuses_a_write_scope_before_launch(tmp_path: Path) -> None:
    role = config.Role(
        name="auditor",
        level="standard",
        forbids_allowed_writes=True,
    )

    with pytest.raises(tasks.SubmitRefused, match="read-only"):
        tasks.submit(
            project_root=tmp_path,
            settings=settings(role),
            role=role.name,
            title="audit",
            value=packet(allowed_writes=["notes.md"]),
        )


@pytest.mark.parametrize(
    "read", [["/mnt/data/run.json"], ["../other/README.md"], ["https://x.test"]]
)
def test_repo_local_role_refuses_external_reads_before_launch(
    tmp_path: Path, read: list[str]
) -> None:
    role = config.Role(name="auditor", level="standard", repo_local_reads=True)

    with pytest.raises(tasks.SubmitRefused, match="repository-local reads only"):
        tasks.submit(
            project_root=tmp_path,
            settings=settings(role),
            role=role.name,
            title="audit",
            value=packet(read=read),
        )


def test_external_read_role_accepts_an_absolute_read(tmp_path: Path) -> None:
    assert tasks._external_reads(["/mnt/data"]) == ["/mnt/data"]
    assert tasks._external_reads(["README.md"]) == []


def test_repo_local_role_allows_only_configured_external_read_roots(tmp_path: Path) -> None:
    allowed_root = tmp_path / "mounted-data"
    allowed_root.mkdir()
    allowed_file = allowed_root / "run.json"
    allowed_file.write_text("{}", encoding="utf-8")
    role = config.Role(
        name="auditor",
        level="standard",
        repo_local_reads=True,
        allowed_read_roots=(str(allowed_root),),
    )

    tasks.submit(
        project_root=tmp_path,
        settings=settings(role),
        role=role.name,
        title="allowed mounted data",
        value=packet(read=[str(allowed_file)]),
    )

    with pytest.raises(tasks.SubmitRefused, match="allowed_read_roots"):
        tasks.submit(
            project_root=tmp_path,
            settings=settings(role),
            role=role.name,
            title="outside mounted data",
            value=packet(read=["/mnt/not-allowed/run.json"]),
        )


def test_repo_local_role_rejects_a_path_that_escapes_an_allowed_root_via_symlink(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "mounted-data"
    allowed_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (allowed_root / "escape.json").symlink_to(outside)
    role = config.Role(
        name="auditor",
        level="standard",
        repo_local_reads=True,
        allowed_read_roots=(str(allowed_root),),
    )

    with pytest.raises(tasks.SubmitRefused, match="allowed_read_roots"):
        tasks.submit(
            project_root=tmp_path,
            settings=settings(role),
            role=role.name,
            title="symlink escape",
            value=packet(read=[str(allowed_root / "escape.json")]),
        )


@pytest.mark.parametrize("path", ["/tmp/result.md", "../result.md", "~/result.md"])
def test_write_scope_must_be_repository_relative(tmp_path: Path, path: str) -> None:
    role = config.Role(name="patcher", level="standard", requires_allowed_writes=True)

    with pytest.raises(tasks.SubmitRefused, match="repository-relative"):
        tasks.submit(
            project_root=tmp_path,
            settings=settings(role),
            role=role.name,
            title="patch",
            value=packet(allowed_writes=[path]),
        )
