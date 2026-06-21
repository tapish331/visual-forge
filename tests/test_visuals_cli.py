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


def test_add_visual_writes_planned_record(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = add_visual(project_dir)

    assert result.returncode == 0, result.stderr
    data = load_project(project_dir)
    visuals = record_list(data, "visuals")
    assert len(visuals) == 1
    visual = visuals[0]
    assert isinstance(visual["id"], str)
    assert visual["id"].startswith("visual_")
    assert visual["template_ref"] == "simple_card"
    assert visual["template_id"] == "simple_card"
    assert visual["status"] == "planned"
    assert visual["start"] == 12.5
    assert visual["end"] == 18.0
    assert visual["preview_id"] is None


def test_add_visual_with_chunk_writes_chunk_scoped_record(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    add_chunk(project_dir, "chunk_001", 10.0, 30.0)
    data = load_project(project_dir)
    record_list(data, "chunks")[0]["status"] = "rendered"
    write_project(project_dir, data)

    result = add_visual(project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["chunk_id"] == "chunk_001"
    data = load_project(project_dir)
    visual = record_list(data, "visuals")[0]
    assert visual["chunk_id"] == "chunk_001"
    assert visual["start"] == 12.5
    assert visual["end"] == 18.0
    assert record_list(data, "chunks")[0]["status"] == "new"


def test_add_visual_rerun_updates_without_duplicate(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    first = add_visual(project_dir, "--json")
    second = add_visual(project_dir, "--json")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_summary = json_object(first.stdout)
    second_summary = json_object(second.stdout)
    assert first_summary["visual_id"] == second_summary["visual_id"]
    data = load_project(project_dir)
    visuals = record_list(data, "visuals")
    assert len(visuals) == 1


def test_add_visual_with_chunk_rerun_updates_without_duplicate(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    add_chunk(project_dir, "chunk_001", 10.0, 30.0)

    first = add_visual(project_dir, "--chunk", "chunk_001", "--json")
    second = add_visual(project_dir, "--chunk", "chunk_001", "--json")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_summary = json_object(first.stdout)
    second_summary = json_object(second.stdout)
    assert first_summary["visual_id"] == second_summary["visual_id"]
    data = load_project(project_dir)
    visuals = record_list(data, "visuals")
    assert len(visuals) == 1
    assert visuals[0]["chunk_id"] == "chunk_001"


def test_visuals_json_returns_compact_summary(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    assert add_visual(project_dir).returncode == 0

    result = run_cli("visuals", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["total"] == 1
    by_status = object_dict(summary, "by_status")
    assert by_status["planned"] == 1
    visuals = record_list(summary, "visuals")
    assert len(visuals) == 1


def test_visuals_json_filters_by_chunk(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    add_chunk(project_dir, "chunk_001", 10.0, 30.0)
    add_chunk(project_dir, "chunk_002", 30.0, 60.0)
    assert add_visual(project_dir, "--chunk", "chunk_001").returncode == 0
    assert add_visual(
        project_dir,
        "--chunk",
        "chunk_002",
        "--start",
        "35",
        "--end",
        "40",
    ).returncode == 0

    result = run_cli("visuals", project_dir, "--chunk", "chunk_001", "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["chunk_id"] == "chunk_001"
    assert summary["total"] == 1
    visuals = record_list(summary, "visuals")
    assert len(visuals) == 1
    assert visuals[0]["chunk_id"] == "chunk_001"


def test_visuals_human_output_lists_visual(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    assert add_visual(project_dir).returncode == 0

    result = run_cli("visuals", project_dir)

    assert result.returncode == 0, result.stderr
    assert "Visuals: 1" in result.stdout
    assert "visual_" in result.stdout
    assert "12.5 -> 18 planned simple_card" in result.stdout


def test_visuals_human_output_includes_chunk_id(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    add_chunk(project_dir, "chunk_001", 10.0, 30.0)
    assert add_visual(project_dir, "--chunk", "chunk_001").returncode == 0

    result = run_cli("visuals", project_dir, "--chunk", "chunk_001")

    assert result.returncode == 0, result.stderr
    assert "Visuals for chunk_001: 1" in result.stdout
    assert "chunk=chunk_001" in result.stdout


def test_status_json_includes_visual_counts(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    assert add_visual(project_dir).returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    visuals = object_dict(summary, "visuals")
    assert visuals["total"] == 1
    by_status = object_dict(visuals, "by_status")
    assert by_status["planned"] == 1


def test_status_json_includes_visual_counts_by_chunk(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    add_chunk(project_dir, "chunk_001", 10.0, 30.0)
    assert add_visual(project_dir, "--chunk", "chunk_001").returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    visuals = object_dict(summary, "visuals")
    by_chunk = object_dict(visuals, "by_chunk")
    assert by_chunk["chunk_001"] == 1


def test_add_visual_normalizes_existing_project_without_visuals(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    data = load_project(project_dir)
    data.pop("visuals")
    write_project(project_dir, data)

    result = add_visual(project_dir)

    assert result.returncode == 0, result.stderr
    updated = load_project(project_dir)
    assert len(record_list(updated, "visuals")) == 1


def test_add_visual_invalid_time_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = run_cli(
        "add-visual",
        project_dir,
        "--template",
        "simple_card",
        "--start",
        "18",
        "--end",
        "12.5",
        "--params-json",
        '{"title":"Bad time"}',
    )

    assert result.returncode != 0
    assert "end must be greater than start" in result.stdout
    data = load_project(project_dir)
    assert data["visuals"] == []
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "add_visual"


def test_add_visual_missing_chunk_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = add_visual(project_dir, "--chunk", "chunk_missing")

    assert result.returncode != 0
    assert "Chunk not found: chunk_missing" in result.stdout
    data = load_project(project_dir)
    assert data["visuals"] == []
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "add_visual"
    assert failures[0]["chunk_id"] == "chunk_missing"


def test_add_visual_outside_chunk_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    add_chunk(project_dir, "chunk_001", 20.0, 30.0)

    result = add_visual(project_dir, "--chunk", "chunk_001")

    assert result.returncode != 0
    assert "visual timing must fit within chunk chunk_001" in result.stdout
    data = load_project(project_dir)
    assert data["visuals"] == []
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "add_visual"
    assert failures[0]["chunk_id"] == "chunk_001"


def test_add_visual_invalid_params_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
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
        '{"subtitle":"Missing title"}',
    )

    assert result.returncode != 0
    assert "title must be a non-empty string" in result.stdout
    data = load_project(project_dir)
    assert data["visuals"] == []
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "add_visual"


def test_add_visual_missing_template_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = run_cli(
        "add-visual",
        project_dir,
        "--template",
        "does_not_exist",
        "--start",
        "12.5",
        "--end",
        "18",
        "--params-json",
        '{"title":"Missing template"}',
    )

    assert result.returncode != 0
    assert "Template not found: does_not_exist" in result.stdout
    data = load_project(project_dir)
    assert data["visuals"] == []
    failures = record_list(data, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "add_visual"
    assert failures[0]["template_ref"] == "does_not_exist"


def add_visual(project_dir: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return run_cli(
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
        *extra_args,
    )


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
