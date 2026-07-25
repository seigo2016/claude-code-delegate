import json
from pathlib import Path

from delegate import store


def test_write_json_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    store.write_json(target, {"status": "queued"})

    assert store.read_json(target) == {"status": "queued"}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_append_jsonl_writes_one_line_per_call_and_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "events.jsonl"

    store.append_jsonl(target, {"status": "starting"})
    store.append_jsonl(target, {"status": "running"})

    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["status"] for line in lines] == ["starting", "running"]
