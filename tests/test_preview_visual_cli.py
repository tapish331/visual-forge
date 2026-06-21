from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path
from typing import TypeAlias, cast


REPO_ROOT = Path(__file__).resolve().parents[1]
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
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


def test_preview_visual_renders_and_updates_visual_record(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)

    result = run_cli("preview-visual", project_dir, visual_id)

    assert result.returncode == 0, result.stderr
    data = load_project(project_dir)
    visual = record_list(data, "visuals")[0]
    preview_id = visual["preview_id"]
    assert isinstance(preview_id, str)
    assert preview_id.startswith("preview_")
    assert visual["status"] == "previewed"
    assert isinstance(visual["updated_at"], str)

    previews = record_list(data, "previews")
    assert len(previews) == 1
    preview = previews[0]
    assert preview["id"] == preview_id
    output_value = preview["output"]
    assert isinstance(output_value, str)
    output = project_dir / output_value
    assert output.exists()
    assert read_png_size(output) == (1920, 1080)


def test_preview_visual_json_returns_compact_summary(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)

    result = run_cli("preview-visual", project_dir, visual_id, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    assert summary["visual_id"] == visual_id
    assert isinstance(summary["preview_id"], str)
    output_path = summary["output_path"]
    assert isinstance(output_path, str)
    assert Path(output_path).exists()
    assert summary["errors"] == []


def test_preview_visual_works_for_chunk_scoped_visual(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    add_chunk(project_dir, "chunk_001", 10.0, 30.0)
    visual_id = create_chunked_visual(project_dir, "chunk_001")

    result = run_cli("preview-visual", project_dir, visual_id, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    assert summary["success"] is True
    data = load_project(project_dir)
    visual = record_list(data, "visuals")[0]
    assert visual["chunk_id"] == "chunk_001"
    assert visual["status"] == "previewed"
    assert isinstance(visual["preview_id"], str)


def test_preview_visual_rerun_reuses_preview_without_duplicate(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)

    first = run_cli("preview-visual", project_dir, visual_id, "--json")
    second = run_cli("preview-visual", project_dir, visual_id, "--json")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_summary = json_object(first.stdout)
    second_summary = json_object(second.stdout)
    assert first_summary["preview_id"] == second_summary["preview_id"]
    data = load_project(project_dir)
    assert len(record_list(data, "previews")) == 1
    assert len(record_list(data, "visuals")) == 1


def test_preview_visual_missing_visual_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)

    result = run_cli("preview-visual", project_dir, "visual_missing")

    assert result.returncode != 0
    assert "Visual not found: visual_missing" in result.stdout
    data = load_project(project_dir)
    failures = record_list(data, "failures")
    assert len(failures) == 1
    failure = failures[0]
    assert failure["stage"] == "preview_visual"
    assert failure["visual_id"] == "visual_missing"


def test_preview_visual_malformed_record_records_failure_without_visual_change(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    data = load_project(project_dir)
    data["visuals"] = [{"id": "visual_bad", "template_ref": 123, "params": {}}]
    write_project(project_dir, data)

    result = run_cli("preview-visual", project_dir, "visual_bad")

    assert result.returncode != 0
    assert "visual.template_ref must be a non-empty string" in result.stdout
    updated = load_project(project_dir)
    visual = record_list(updated, "visuals")[0]
    assert visual["template_ref"] == 123
    assert "preview_id" not in visual
    failures = record_list(updated, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "preview_visual"


def test_preview_visual_template_validation_failure_records_failure(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    init_project(project_dir)
    data = load_project(project_dir)
    data["visuals"] = [{"id": "visual_invalid", "template_ref": "simple_card", "params": {"subtitle": "Only"}}]
    write_project(project_dir, data)

    result = run_cli("preview-visual", project_dir, "visual_invalid")

    assert result.returncode != 0
    assert "title must be a non-empty string" in result.stdout
    updated = load_project(project_dir)
    visual = record_list(updated, "visuals")[0]
    assert "preview_id" not in visual
    failures = record_list(updated, "failures")
    assert len(failures) == 1
    assert failures[0]["stage"] == "preview_visual"
    assert failures[0]["template_ref"] == "simple_card"


def test_preview_visual_updates_status_counts(tmp_path: Path) -> None:
    project_dir = tmp_path / "my-video"
    visual_id = create_visual(project_dir)
    assert run_cli("preview-visual", project_dir, visual_id).returncode == 0

    result = run_cli("status", project_dir, "--json")

    assert result.returncode == 0, result.stderr
    summary = json_object(result.stdout)
    visuals = object_dict(summary, "visuals")
    by_status = object_dict(visuals, "by_status")
    assert by_status["previewed"] == 1


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


def create_chunked_visual(project_dir: Path, chunk_id: str) -> str:
    result = run_cli(
        "add-visual",
        project_dir,
        "--chunk",
        chunk_id,
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


def read_png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    width_raw, height_raw = struct.unpack(">II", data[16:24])
    assert isinstance(width_raw, int)
    assert isinstance(height_raw, int)
    width = width_raw
    height = height_raw
    return width, height
