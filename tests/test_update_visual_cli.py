from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
ProjectJson: TypeAlias = dict[str, object]
Record: TypeAlias = dict[str, object]


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.main", *(str(arg) for arg in args)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )


def init_project(project_dir: Path) -> None:
    result = run_cli("init", project_dir)
    assert result.returncode == 0, result.stderr


def load_project(project_dir: Path) -> ProjectJson:
    raw: object = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def write_project(project_dir: Path, data: ProjectJson) -> None:
    (project_dir / "project.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def test_update_visual_params_resets_preview_and_records_correction(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_previewed_visual(project_dir)
    before = load_project(project_dir)
    before_visual = record_list(before, "visuals")[0]
    created_at = before_visual["created_at"]
    old_preview_id = before_visual["preview_id"]
    assert isinstance(old_preview_id, str)

    result = run_cli(
        "update-visual",
        project_dir,
        visual_id,
        "--params-json",
        '{"title":"Better title"}',
        "--json",
    )

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["visual_id"] == visual_id
    assert string_list(summary, "changes") == ["params"]

    data = load_project(project_dir)
    visual = record_list(data, "visuals")[0]
    assert visual["id"] == visual_id
    assert visual["status"] == "planned"
    assert visual["preview_id"] is None
    assert visual["created_at"] == created_at
    params = object_dict(visual, "params")
    assert params["title"] == "Better title"

    previews = record_list(data, "previews")
    assert len(previews) == 1
    assert previews[0]["id"] == old_preview_id

    corrections = record_list(data, "corrections")
    assert len(corrections) == 1
    correction = corrections[0]
    assert correction["stage"] == "visual"
    assert correction["visual_id"] == visual_id
    changes = object_dict(correction, "changes")
    params_change = object_dict(changes, "params")
    before_params = object_dict(params_change, "before")
    after_params = object_dict(params_change, "after")
    assert before_params["title"] == "Key idea"
    assert after_params["title"] == "Better title"


def test_update_visual_timing_preserves_created_at(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)
    before_visual = record_list(load_project(project_dir), "visuals")[0]
    created_at = before_visual["created_at"]

    result = run_cli("update-visual", project_dir, visual_id, "--start", "14", "--end", "19")

    assert result.returncode == 0, result.stderr
    data = load_project(project_dir)
    visual = record_list(data, "visuals")[0]
    assert visual["id"] == visual_id
    assert visual["created_at"] == created_at
    assert visual["start"] == 14.0
    assert visual["end"] == 19.0
    assert visual["status"] == "planned"
    assert visual["preview_id"] is None

    corrections = record_list(data, "corrections")
    assert len(corrections) == 1
    changes = object_dict(corrections[0], "changes")
    assert object_dict(changes, "start")["after"] == 14.0
    assert object_dict(changes, "end")["after"] == 19.0


def test_update_visual_template_and_params_revalidates(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)

    result = run_cli(
        "update-visual",
        project_dir,
        visual_id,
        "--template",
        "simple_card",
        "--params-json",
        '{"title":"Reworked visual","subtitle":"Still simple"}',
        "--json",
    )

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert string_list(summary, "changes") == ["params"]
    visual = record_list(load_project(project_dir), "visuals")[0]
    assert visual["template_ref"] == "simple_card"
    params = object_dict(visual, "params")
    assert params["title"] == "Reworked visual"
    assert params["subtitle"] == "Still simple"


def test_update_visual_chunk_moves_visual_and_records_correction(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    add_chunk(project_dir, "chunk_001", 10.0, 30.0)
    add_chunk(project_dir, "chunk_002", 30.0, 60.0)
    visual_id = add_chunked_visual(project_dir, "chunk_001", 12.5, 18.0)
    before = load_project(project_dir)
    for chunk in record_list(before, "chunks"):
        chunk["status"] = "rendered"
    write_project(project_dir, before)

    result = run_cli(
        "update-visual",
        project_dir,
        visual_id,
        "--chunk",
        "chunk_002",
        "--start",
        "35",
        "--end",
        "40",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["chunk_id"] == "chunk_002"
    assert string_list(summary, "changes") == ["chunk_id", "start", "end"]
    updated = load_project(project_dir)
    visual = record_list(updated, "visuals")[0]
    assert visual["chunk_id"] == "chunk_002"
    assert visual["start"] == 35.0
    assert visual["end"] == 40.0
    corrections = record_list(load_project(project_dir), "corrections")
    changes = object_dict(corrections[0], "changes")
    assert object_dict(changes, "chunk_id")["before"] == "chunk_001"
    assert object_dict(changes, "chunk_id")["after"] == "chunk_002"
    updated_chunks = record_list(updated, "chunks")
    assert updated_chunks[0]["status"] == "new"
    assert updated_chunks[1]["status"] == "new"


def test_update_visual_json_returns_compact_summary(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)

    result = run_cli("update-visual", project_dir, visual_id, "--params-json", '{"title":"JSON"}', "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["project_dir"] == str(project_dir)
    assert summary["visual_id"] == visual_id
    assert summary["success"] is True
    assert string_list(summary, "changes") == ["params"]
    assert summary["errors"] == []


def test_update_visual_preserves_chunk_and_rejects_out_of_chunk_timing(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    add_chunk(project_dir, "chunk_001", 10.0, 30.0)
    visual_id = add_chunked_visual(project_dir, "chunk_001", 12.5, 18.0)
    original_visual = dict(record_list(load_project(project_dir), "visuals")[0])

    result = run_cli("update-visual", project_dir, visual_id, "--start", "31", "--end", "35")

    assert result.returncode != 0
    assert "visual timing must fit within chunk chunk_001" in result.stdout
    data = load_project(project_dir)
    assert record_list(data, "visuals")[0] == original_visual
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "update_visual"
    assert failures[0]["chunk_id"] == "chunk_001"


def test_update_visual_no_options_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)

    result = run_cli("update-visual", project_dir, visual_id)

    assert result.returncode != 0
    assert "At least one update option is required" in result.stdout
    data = load_project(project_dir)
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "update_visual"
    assert failures[0]["visual_id"] == visual_id


def test_update_visual_missing_visual_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = run_cli("update-visual", project_dir, "visual_missing", "--params-json", '{"title":"Missing"}')

    assert result.returncode != 0
    assert "Visual not found: visual_missing" in result.stdout
    failures = record_list(load_project(project_dir), "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "update_visual"
    assert failures[0]["visual_id"] == "visual_missing"


def test_update_visual_malformed_record_records_failure_without_visual_change(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    data = load_project(project_dir)
    malformed: Record = {"id": "visual_bad", "template_ref": "simple_card", "params": {"title": "Bad"}}
    data["visuals"] = [malformed]
    write_project(project_dir, data)

    result = run_cli("update-visual", project_dir, "visual_bad", "--params-json", '{"title":"Better"}')

    assert result.returncode != 0
    assert "visual.start must be a number" in result.stdout
    updated = load_project(project_dir)
    assert record_list(updated, "visuals")[0] == malformed
    failures = record_list(updated, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "update_visual"


def test_update_visual_invalid_params_leaves_visual_unchanged(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)
    original_visual = dict(record_list(load_project(project_dir), "visuals")[0])

    result = run_cli("update-visual", project_dir, visual_id, "--params-json", '{"subtitle":"Missing title"}')

    assert result.returncode != 0
    assert "title must be a non-empty string" in result.stdout
    data = load_project(project_dir)
    assert record_list(data, "visuals")[0] == original_visual
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "update_visual"


def test_update_visual_invalid_time_leaves_visual_unchanged(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)
    original_visual = dict(record_list(load_project(project_dir), "visuals")[0])

    result = run_cli("update-visual", project_dir, visual_id, "--start", "20", "--end", "10")

    assert result.returncode != 0
    assert "end must be greater than start" in result.stdout
    data = load_project(project_dir)
    assert record_list(data, "visuals")[0] == original_visual
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "update_visual"


def create_previewed_visual(project_dir: Path) -> str:
    visual_id = create_visual(project_dir)
    result = run_cli("preview-visual", project_dir, visual_id, "--json")
    assert result.returncode == 0, result.stderr
    return visual_id


def create_visual(project_dir: Path) -> str:
    init_project(project_dir)
    result = run_cli(
        "add-visual",
        project_dir,
        "--template",
        "simple_card",
        "--start",
        "12.5",
        "--end",
        "18",
        "--params-json",
        '{"title":"Key idea"}',
        "--json",
    )
    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    visual_id = summary["visual_id"]
    assert isinstance(visual_id, str)
    return visual_id


def add_chunked_visual(project_dir: Path, chunk_id: str, start: float, end: float) -> str:
    result = run_cli(
        "add-visual",
        project_dir,
        "--chunk",
        chunk_id,
        "--template",
        "simple_card",
        "--start",
        str(start),
        "--end",
        str(end),
        "--params-json",
        '{"title":"Key idea"}',
        "--json",
    )
    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    visual_id = summary["visual_id"]
    assert isinstance(visual_id, str)
    return visual_id


def add_chunk(project_dir: Path, chunk_id: str, start: float, end: float) -> None:
    data = load_project(project_dir)
    chunks = data["chunks"]
    assert isinstance(chunks, list)
    chunks.append(
        {
            "id": chunk_id,
            "start": start,
            "end": end,
            "status": "new",
            "alignment_block_ids": [],
            "warning_block_ids": [],
            "created_at": "2026-06-20T00:00:00Z",
            "updated_at": "2026-06-20T00:00:00Z",
        }
    )
    write_project(project_dir, data)


def json_object(raw_json: str) -> ProjectJson:
    raw: object = json.loads(raw_json)
    assert isinstance(raw, dict)
    return cast(ProjectJson, raw)


def object_dict(data: ProjectJson | Record, key: str) -> Record:
    value = data[key]
    assert isinstance(value, dict)
    return cast(Record, value)


def record_list(data: ProjectJson | Record, key: str) -> list[Record]:
    value = data[key]
    assert isinstance(value, list)
    items = cast(list[object], value)
    records: list[Record] = []
    for item in items:
        assert isinstance(item, dict)
        records.append(cast(Record, item))
    return records


def string_list(data: ProjectJson | Record, key: str) -> list[str]:
    value = data[key]
    assert isinstance(value, list)
    items = cast(list[object], value)
    strings: list[str] = []
    for item in items:
        assert isinstance(item, str)
        strings.append(item)
    return strings
