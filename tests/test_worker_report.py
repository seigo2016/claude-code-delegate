from __future__ import annotations

import json
from pathlib import Path

from conftest import Workspace
from delegate import envelope
from test_lifecycle import wait_for_terminal


def test_schema_and_sanitization() -> None:
    props = envelope.json_schema()["properties"]
    assert props["observed_facts"]["maxItems"] >= 200
    assert props["observed_facts"]["items"]["maxLength"] >= 4000
    assert all(key in props for key in ("report", "counts", "coverage"))
    result = {"observed_facts": ["x" * 400] * 8, "coverage": {
        "done": 1, "total": 20, "remaining": [str(i) for i in range(19)]}}
    clean = envelope.sanitize(result)
    assert len(clean["observed_facts"]) == 5
    assert len(clean["observed_facts"][0]) == 300
    assert len(clean["coverage"]["remaining"]) == 19


def test_report_collection_and_command_environment(workspace: Workspace, tmp_path: Path) -> None:
    record = tmp_path / "env.json"
    workspace.env["REPORT_ENV"] = str(record)
    workspace.env["PYTEST_ADDOPTS"] = "-q"
    worker = workspace.worker_path
    script = worker.read_text()
    script = script.replace(
        'event({"type": "thread.started",', '''names = ("TMPDIR", "PYTHONPYCACHEPREFIX",
         "RUFF_CACHE_DIR", "PYTEST_ADDOPTS")
pathlib.Path(os.environ["REPORT_ENV"]).write_text(
    json.dumps({k: os.environ.get(k) for k in names}))
event({"type": "thread.started",''')
    script = script.replace(
        'if "--output-schema" in args:', '''good.update(report="R" * 50000, counts=[{"name": "pass", "count": 3}],
            coverage={"done": 3, "total": 15, "remaining": [str(i) for i in range(12)]})
if "--output-schema" in args:''')
    worker.write_text(script)
    handle = workspace.submit(role="verification-runner")
    state = wait_for_terminal(workspace, handle["task_id"])
    assert state["status"] == "completed"
    delivery = workspace.run("collect", handle["task_id"])
    result = delivery["result"]
    assert Path(result["report_path"]).read_text() == "R" * 50000
    assert result["counts"] == [{"name": "pass", "count": 3}]
    assert len(result["coverage"]["remaining"]) == 10
    assert "report" not in result
    short = {k: v for k, v in result.items() if k not in ("report_path", "counts", "coverage")}
    without = json.dumps({**delivery, "result": short})
    assert len(json.dumps(delivery)) < len(without) + 1000
    env = json.loads(record.read_text())
    assert env["PYTHONPYCACHEPREFIX"].startswith(env["TMPDIR"] + "/")
    assert env["RUFF_CACHE_DIR"].startswith(env["TMPDIR"] + "/")
    assert "-q" in env["PYTEST_ADDOPTS"] and "-p no:cacheprovider" in env["PYTEST_ADDOPTS"]


def test_empty_or_null_report_does_not_create_report_file(tmp_path: Path) -> None:
    from delegate import runner

    for report in (None, ""):
        result_path = tmp_path / "result.json"
        runner._save_result(tmp_path, result_path, {"report": report, "counts": None})
        saved = json.loads(result_path.read_text())
        assert "report" not in saved
        assert "report_path" not in saved
        assert not (tmp_path / "report.md").exists()
