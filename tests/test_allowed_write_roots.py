from delegate import config


def test_role_parses_explicit_external_write_roots(tmp_path):
    settings_path = tmp_path / "delegate.toml"
    settings_path.write_text(
        "[roles.repro-runner]\n"
        'level = "standard"\n'
        "runs_commands = true\n"
        'allowed_write_roots = ["/mnt/f"]\n'
        "[workers.codex]\n"
        'adapter = "codex"\n'
        "enabled = true\n"
        "[workers.codex.standard]\n"
        'model = "test-model"\n'
        'effort = "medium"\n'
    )

    plan = config.load(settings_path).plan("repro-runner")

    assert getattr(plan.role, "allowed_write_roots", None) == ("/mnt/f",)
    assert plan.role.allowed_read_roots == ()
