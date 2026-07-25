from pathlib import Path

from delegate import store


def test_write_json_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    store.write_json(target, {"status": "queued"})

    assert store.read_json(target) == {"status": "queued"}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
